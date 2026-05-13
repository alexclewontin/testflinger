#!/bin/bash
set -eu

echo "=== Microtestflinger Setup ==="
echo ""

# Check MongoDB
        echo "  Install with: sudo snap install charmed-mongodb"
        echo "  Or: sudo apt install mongodb-server-core && sudo systemctl start mongod"
        exit 1
    fi
else
fi

# Create runtime directories
mkdir -p /tmp/microtestflinger/{run,logs,results}
echo "✓ Runtime directories created"

# Copy default agent config
if [ ! -f "$SNAP_DATA/testflinger-agent.conf" ]; then
    cp "$SNAP/etc/microtestflinger/testflinger-agent.conf" "$SNAP_DATA/testflinger-agent.conf"
    echo "✓ Agent config copied to $SNAP_DATA/"
fi

echo ""
echo "=== Setup Complete ==="
echo "Start services:  sudo snap start microtestflinger.server && sudo snap start microtestflinger.agent"
echo "Submit a job:    microtestflinger.cli submit job.yaml"
echo "Check status:    microtestflinger.cli status <job-id>"
