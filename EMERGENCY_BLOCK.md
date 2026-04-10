# Emergency Block — imap.simpleinboxes.com (143.198.126.147)

SSH into the droplet and run the following commands **exactly**:

```bash
# Block IMAP (port 993) at the firewall
sudo ufw deny 993/tcp
sudo ufw reload

# Stop and disable the proxy service
sudo systemctl stop pixie-imap
sudo systemctl disable pixie-imap

# Optional: verify status
sudo systemctl status pixie-imap --no-pager
sudo ufw status numbered
```
