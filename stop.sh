#!/bin/bash
pkill -f clock.py

while pgrep -f clock.py > /dev/null; do
    echo "Waiting for clock.py to close..."
    sleep 1
done

echo "clock.py has been terminated."