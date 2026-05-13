# Copyright (C) 2025 Canonical
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""QEMU device connector for local VM-backed testing."""

import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time

import yaml

import testflinger_device_connectors
from testflinger_device_connectors.devices import DefaultDevice, SerialLogger

logger = logging.getLogger(__name__)

STATE_FILE = "qemu-state.json"
OVERLAY_FILE = "qemu-overlay.qcow2"
SEED_FILE = "qemu-seed.iso"
USER_DATA_FILE = "qemu-user-data"
META_DATA_FILE = "qemu-meta-data"
PID_FILE = "qemu.pid"
TEST_SCRIPT_FILE = "qemu-test-script.sh"
REMOTE_TEST_SCRIPT = "~/qemu-test-script.sh"
SSH_OPTIONS = [
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
]
SSH_READY_TIMEOUT = 300
SSH_READY_INTERVAL = 5
PHASE_ENV_VAR = "QEMU_CONNECTOR_PHASE"


def _load_yaml(path):
    with open(path, encoding="utf-8") as yaml_file:
        return yaml.safe_load(yaml_file) or {}


def _load_json(path):
    with open(path, encoding="utf-8") as json_file:
        return json.load(json_file)


def _state_path():
    return os.path.abspath(STATE_FILE)


def _artifact_paths():
    return {
        "overlay_path": os.path.abspath(OVERLAY_FILE),
        "seed_iso": os.path.abspath(SEED_FILE),
        "user_data_path": os.path.abspath(USER_DATA_FILE),
        "meta_data_path": os.path.abspath(META_DATA_FILE),
        "pidfile": os.path.abspath(PID_FILE),
        "local_test_script": os.path.abspath(TEST_SCRIPT_FILE),
    }


def _resolve_path(config_path, path_value):
    if os.path.isabs(path_value):
        return path_value
    return os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(config_path)), path_value)
    )


def _ensure_parent_dir(path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _write_file(path, content):
    _ensure_parent_dir(path)
    with open(path, "w", encoding="utf-8") as file_handle:
        file_handle.write(content)


def _remove_file(path):
    if path and os.path.exists(path):
        os.unlink(path)


def _load_state():
    state_file = _state_path()
    if not os.path.exists(state_file):
        return {}
    return _load_json(state_file)


def _write_state(state):
    _write_file(_state_path(), json.dumps(state, indent=2))


def _normalize_cmds(cmds, config):
    if not cmds:
        return []
    if isinstance(cmds, list):
        lines = []
        for cmd in cmds:
            if "{{" in cmd:
                cmd = (
                    testflinger_device_connectors._process_cmds_template_vars(
                        cmd, config
                    )
                )
            lines.append(cmd)
        return lines
    if isinstance(cmds, str):
        if "{{" in cmds:
            cmds = testflinger_device_connectors._process_cmds_template_vars(
                cmds, config
            )
        lines = cmds.splitlines()
        if lines and lines[0].startswith("#!"):
            lines = lines[1:]
        return lines
    msg = "setup_cmds/test_cmds field must be a list or string"
    raise TypeError(msg)


def _stream_command(cmd):
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as process:
        if process.stdout:
            for line in process.stdout:
                sys.stdout.write(line)
        return process.wait()


# Ubuntu cloud image URL template and known distro name/codename mappings
_UBUNTU_IMAGE_URL = (
    "https://cloud-images.ubuntu.com/{series}/current/"
    "{series}-server-cloudimg-amd64.img"
)
_DISTRO_TO_SERIES = {
    # codenames
    "noble": "noble",
    "jammy": "jammy",
    "focal": "focal",
    "bionic": "bionic",
    # version numbers
    "24.04": "noble",
    "22.04": "jammy",
    "20.04": "focal",
    "18.04": "bionic",
}


def _image_for_distro(distro: str, image_dir: str) -> str:
    """Return local path to cloud image for *distro*, downloading if needed."""
    series = _DISTRO_TO_SERIES.get(distro.lower())
    if not series:
        raise ValueError(
            f"Unknown distro '{distro}'. "
            f"Known values: {sorted(_DISTRO_TO_SERIES)}"
        )
    filename = f"{series}-server-cloudimg-amd64.img"
    local_path = os.path.join(image_dir, filename)
    if not os.path.exists(local_path):
        url = _UBUNTU_IMAGE_URL.format(series=series)
        logger.info("Downloading cloud image %s → %s", url, local_path)
        os.makedirs(image_dir, exist_ok=True)
        _download_file(url, local_path)
    return local_path


def _download_file(url: str, dest: str) -> None:
    """Download *url* to *dest*, using curl if available else urllib."""
    curl = shutil.which("curl")
    if curl:
        subprocess.run(
            [curl, "-fsSL", url, "-o", dest],
            check=True,
        )
        return
    import urllib.request  # noqa: PLC0415
    urllib.request.urlretrieve(url, dest)


def _cloud_init_user_data(ssh_user, ssh_pubkey, packages):
    user_data = {
        "users": [
            {
                "name": ssh_user,
                "gecos": ssh_user,
                "shell": "/bin/bash",
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
                "groups": ["adm", "sudo"],
                "lock_passwd": True,
                "ssh_authorized_keys": [ssh_pubkey],
            }
        ],
        "ssh_pwauth": False,
        "disable_root": True,
        "package_update": bool(packages),
    }
    if packages:
        user_data["packages"] = packages
    return "#cloud-config\n" + yaml.safe_dump(user_data, sort_keys=False)


def _cloud_init_meta_data(job_data):
    job_id = job_data.get("job_id", "microtestflinger")
    return (
        f"instance-id: microtestflinger-{job_id}\n"
        f"local-hostname: microtestflinger-{job_id}\n"
    )


def _render_template(template_path, replacements):
    with open(template_path, encoding="utf-8") as template_file:
        rendered = template_file.read()

    for key, value in replacements.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(value))

    return rendered


def _write_cloud_init_files(config_path, config, job_data, artifacts, ssh_user, ssh_pubkey, packages):
    job_id = job_data.get("job_id", "microtestflinger")
    replacements = {
        "SSH_PUBLIC_KEY": ssh_pubkey,
        "SSH_USER": ssh_user,
        "INSTANCE_ID": f"microtestflinger-{job_id}",
        "LOCAL_HOSTNAME": f"microtestflinger-{job_id}",
    }

    user_data_template = config.get("cloud_init_user_data")
    if user_data_template:
        user_data = _render_template(
            _resolve_path(config_path, user_data_template), replacements
        )
    else:
        user_data = _cloud_init_user_data(ssh_user, ssh_pubkey, packages)

    meta_data_template = config.get("cloud_init_meta_data")
    if meta_data_template:
        meta_data = _render_template(
            _resolve_path(config_path, meta_data_template), replacements
        )
    else:
        meta_data = _cloud_init_meta_data(job_data)

    _write_file(artifacts["user_data_path"], user_data)
    _write_file(artifacts["meta_data_path"], meta_data)


def _ensure_ssh_keypair(private_key_path):
    public_key_path = f"{private_key_path}.pub"
    if os.path.exists(private_key_path) and os.path.exists(public_key_path):
        return public_key_path

    _ensure_parent_dir(private_key_path)
    subprocess.run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-f",
            private_key_path,
        ],
        check=True,
    )
    return public_key_path


def _create_seed_image(seed_iso, user_data_path, meta_data_path):
    cloud_localds = shutil.which("cloud-localds")
    if cloud_localds:
        subprocess.run(
            [cloud_localds, seed_iso, user_data_path, meta_data_path],
            check=True,
        )
        return

    genisoimage = shutil.which("genisoimage") or shutil.which("mkisofs")
    if genisoimage:
        subprocess.run(
            [
                genisoimage,
                "-output",
                seed_iso,
                "-volid",
                "cidata",
                "-joliet",
                "-rock",
                user_data_path,
                meta_data_path,
            ],
            check=True,
        )
        return

    msg = "cloud-localds or genisoimage is required to create cloud-init media"
    raise FileNotFoundError(msg)


def _active_phase():
    phase = os.environ.get(PHASE_ENV_VAR, "").strip().lower()
    if phase in {"setup", "test"}:
        return phase
    return None


def _read_job_cmds(test_opportunity, key, phase=None):
    if phase:
        phase_data = test_opportunity.get(f"{phase}_data", {})
        return phase_data.get(key) or test_opportunity.get(key)

    test_data = test_opportunity.get("test_data", {})
    setup_data = test_opportunity.get("setup_data", {})
    return (
        test_data.get(key)
        or setup_data.get(key)
        or test_opportunity.get(key)
    )


def _job_environment(test_opportunity, config, phase=None):
    env = {}
    env.update(config.get("env", {}))
    env.update(test_opportunity.get("environment", {}))
    env.update(test_opportunity.get("env", {}))

    phase_sections = ["setup_data", "test_data"]
    if phase:
        phase_sections = [f"{phase}_data"]

    for phase_section in phase_sections:
        phase_data = test_opportunity.get(phase_section, {})
        env.update(phase_data.get("environment", {}))
        env.update(phase_data.get("env", {}))
        env.update(phase_data.get("secrets", {}))

    env.update(
        {
            "AGENT_NAME": config.get("agent_name", ""),
            "REBOOT_SCRIPT": ";".join(config.get("reboot_script", [])),
        }
    )
    return {
        str(key): str(value)
        for key, value in env.items()
        if value is not None
    }


def _write_test_script(path, env, setup_cmds, test_cmds, config):
    lines = ["#!/bin/bash", "set -e", ""]
    for key, value in sorted(env.items()):
        lines.append(f"export {key}={shlex.quote(value)}")

    setup_lines = _normalize_cmds(setup_cmds, config)
    test_lines = _normalize_cmds(test_cmds, config)
    if not setup_lines and not test_lines:
        msg = "No setup_cmds or test_cmds found in job data"
        raise ValueError(msg)

    if setup_lines:
        lines.extend(["", "# setup_cmds", *setup_lines])
    if test_lines:
        lines.extend(["", "# test_cmds", *test_lines])

    _write_file(path, "\n".join(lines) + "\n")
    os.chmod(path, 0o700)  # noqa: S103


def _ssh_base_cmd(ssh_user, ssh_port, ssh_key):
    return [
        "ssh",
        "-p",
        str(ssh_port),
        "-i",
        ssh_key,
        *SSH_OPTIONS,
        f"{ssh_user}@localhost",
    ]


def _scp_base_cmd(ssh_user, ssh_port, ssh_key):
    return [
        "scp",
        "-P",
        str(ssh_port),
        "-i",
        ssh_key,
        *SSH_OPTIONS,
    ]


def _wait_for_ssh(ssh_user, ssh_port, ssh_key):
    deadline = time.time() + SSH_READY_TIMEOUT
    cmd = [
        *_ssh_base_cmd(ssh_user, ssh_port, ssh_key),
        "-o",
        "ConnectTimeout=5",
        "true",
    ]
    while time.time() < deadline:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        time.sleep(SSH_READY_INTERVAL)

    msg = f"Timed out waiting for SSH on localhost:{ssh_port}"
    raise TimeoutError(msg)


def _stop_process(pid):
    if not pid:
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(1)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _cleanup_artifacts(state):
    _stop_process(state.get("pid"))
    for key in (
        "overlay_path",
        "seed_iso",
        "user_data_path",
        "meta_data_path",
        "pidfile",
        "local_test_script",
    ):
        _remove_file(state.get(key))
    _remove_file(_state_path())


class DeviceConnector(DefaultDevice):
    """QEMU-backed device connector."""

    def provision(self, args):
        """Launch a QEMU VM with an Ubuntu cloud image."""
        super().provision(args)

        config = _load_yaml(args.config)
        test_opportunity = testflinger_device_connectors.get_test_opportunity(
            args.job_data
        )
        provision_data = test_opportunity.get("provision_data", {})
        artifacts = _artifact_paths()

        ssh_key = _resolve_path(args.config, config["ssh_key"])
        ssh_user = config.get("ssh_user", "ubuntu")
        ssh_port = int(config.get("ssh_port", 2222))
        qemu_memory = str(config.get("qemu_memory", 2048))
        qemu_cpus = str(config.get("qemu_cpus", 2))
        image_dir = config.get("qemu_image_dir", "/tmp/microtestflinger/images")
        distro = provision_data.get("distro", "noble")
        packages = provision_data.get("packages") or provision_data.get(
            "apt_packages", []
        )
        # Display config: provision_data overrides device config
        qemu_display = provision_data.get(
            "qemu_display", config.get("qemu_display", "virtio-vga")
        )
        # VNC config: set to a display number like ":0" (port 5900), ":1" (5901)
        qemu_vnc = provision_data.get(
            "qemu_vnc", config.get("qemu_vnc")
        )

        logger.info("BEGIN provision")
        if os.path.exists(_state_path()):
            _cleanup_artifacts(_load_state())
        for path in artifacts.values():
            _remove_file(path)

        public_key = _ensure_ssh_keypair(ssh_key)
        with open(public_key, encoding="utf-8") as public_key_file:
            ssh_pubkey = public_key_file.read().strip()

        base_image = _image_for_distro(distro, image_dir)

        qemu_img = shutil.which("qemu-img")
        qemu_system = shutil.which("qemu-system-x86_64")
        if not qemu_img or not qemu_system:
            msg = "qemu-img and qemu-system-x86_64 are required"
            raise FileNotFoundError(msg)

        state = {
            **artifacts,
            "ssh_key": ssh_key,
            "ssh_port": ssh_port,
            "ssh_user": ssh_user,
            "base_image": base_image,
        }

        try:
            subprocess.run(
                [
                    qemu_img,
                    "create",
                    "-f",
                    "qcow2",
                    "-b",
                    base_image,
                    "-F",
                    "qcow2",
                    artifacts["overlay_path"],
                ],
                check=True,
            )

            _write_cloud_init_files(
                args.config,
                config,
                test_opportunity,
                artifacts,
                ssh_user,
                ssh_pubkey,
                packages,
            )
            _create_seed_image(
                artifacts["seed_iso"],
                artifacts["user_data_path"],
                artifacts["meta_data_path"],
            )

            qemu_cmd = [
                qemu_system,
                "-m",
                qemu_memory,
                "-smp",
                qemu_cpus,
                "-enable-kvm",
                "-drive",
                f"file={artifacts['overlay_path']},format=qcow2",
                "-cdrom",
                artifacts["seed_iso"],
                "-net",
                "nic",
                "-net",
                f"user,hostfwd=tcp::{ssh_port}-:22",
                "-daemonize",
                "-pidfile",
                artifacts["pidfile"],
                "-device",
                str(qemu_display),
            ]
            if qemu_vnc:
                qemu_cmd.extend(["-vnc", str(qemu_vnc)])
            else:
                qemu_cmd.extend(["-display", "none"])

            logger.info("QEMU command: %s", " ".join(qemu_cmd))
            subprocess.run(qemu_cmd, check=True)

            with open(artifacts["pidfile"], encoding="utf-8") as pid_file:
                state["pid"] = int(pid_file.read().strip())

            _wait_for_ssh(ssh_user, ssh_port, ssh_key)
            _write_state(state)
        except Exception:
            _cleanup_artifacts(state)
            raise
        finally:
            logger.info("END provision")

    def runtest(self, args):
        """Run test commands inside the QEMU VM via SSH."""
        config = _load_yaml(args.config)
        test_opportunity = testflinger_device_connectors.get_test_opportunity(
            args.job_data
        )
        state = _load_state()

        logger.info("BEGIN testrun")

        ssh_key = state.get("ssh_key") or _resolve_path(
            args.config, config["ssh_key"]
        )
        ssh_port = int(state.get("ssh_port", config.get("ssh_port", 2222)))
        ssh_user = state.get("ssh_user", config.get("ssh_user", "ubuntu"))
        phase = _active_phase()
        setup_cmds = _read_job_cmds(test_opportunity, "setup_cmds", phase=phase)
        test_cmds = _read_job_cmds(test_opportunity, "test_cmds", phase=phase)
        if phase == "setup":
            test_cmds = None
        elif phase == "test":
            setup_cmds = None

        serial_host = config.get("serial_host")
        serial_port = config.get("serial_port")
        serial_proc = SerialLogger(serial_host, serial_port, "test-serial.log")
        serial_proc.start()

        state.update(
            {
                "ssh_key": ssh_key,
                "ssh_port": ssh_port,
                "ssh_user": ssh_user,
                "local_test_script": state.get(
                    "local_test_script", _artifact_paths()["local_test_script"]
                ),
            }
        )

        try:
            _write_test_script(
                state["local_test_script"],
                _job_environment(test_opportunity, config, phase=phase),
                setup_cmds,
                test_cmds,
                config,
            )

            scp_cmd = [
                *_scp_base_cmd(ssh_user, ssh_port, ssh_key),
                state["local_test_script"],
                f"{ssh_user}@localhost:{REMOTE_TEST_SCRIPT}",
            ]
            exitcode = _stream_command(scp_cmd)
            if exitcode:
                return exitcode

            ssh_cmd = [
                *_ssh_base_cmd(ssh_user, ssh_port, ssh_key),
                f"chmod +x {REMOTE_TEST_SCRIPT} && {REMOTE_TEST_SCRIPT}",
            ]
            return _stream_command(ssh_cmd)
        finally:
            serial_proc.stop()
            logger.info("END testrun")

    def cleanup(self, args):
        """Stop QEMU VM and clean up artifacts."""
        logger.info("BEGIN cleanup")

        state = _artifact_paths()
        state.update(_load_state())

        config = _load_yaml(args.config)
        if "ssh_key" in config:
            state.setdefault(
                "ssh_key", _resolve_path(args.config, config["ssh_key"])
            )

        _cleanup_artifacts(state)
        logger.info("END cleanup")
