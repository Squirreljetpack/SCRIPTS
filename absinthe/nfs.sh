#!/bin/bash

MOUNT_POINT="/Users/rz/internalsync"
REMOTE_DIR="100.79.11.140:/home/archr/internalsync"
OPTIONS="-o soft,rsize=1048576,wsize=1048576,proto=tcp,timeo=45,retrans=3"

# Check if the directory is mounted
if mount | grep "$MOUNT_POINT" >/dev/null; then
    echo "Unmounting $MOUNT_POINT..."
    sudo umount "$MOUNT_POINT"
    RESULT=$?

    # If regular umount fails, try umount -f
    if [ $RESULT -ne 0 ]; then
        echo "Regular umount failed. Trying umount -f..."
        sudo umount -f "$MOUNT_POINT"
        RESULT=$?
    fi

    # If umount -f fails, ask permission to try umount -fl
    if [ $RESULT -ne 0 ]; then
        read -p "umount -f failed. Do you want to try umount -fl? (y/n) " choice
        case "$choice" in
        y | Y) sudo umount -fl "$MOUNT_POINT" ;;
        n | N) echo "Unmounting aborted." ;;
        *) echo "Invalid choice." ;;
        esac
    fi
else
    echo "Mounting $REMOTE_DIR to $MOUNT_POINT..."
    sudo mount $OPTIONS "$REMOTE_DIR" "$MOUNT_POINT"
fi
