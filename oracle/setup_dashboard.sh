#!/usr/bin/env bash
# Serve the dashboard directly from the VM over HTTPS (self-signed), reading
# the results files off the same disk the trader writes to -> real-time, no
# GitHub CDN lag. Run on the VM:  bash oracle/setup_dashboard.sh
#
# Security: nginx serves ONLY docs/index.html and the four data files the
# dashboard reads. config/ (Upstox credentials) is never under a served path
# and is 600 anyway. Only port 443 is opened.
set -euo pipefail

IP="140.238.226.69"
CERT_DIR="/etc/nginx/zen"

sudo apt-get update -qq
sudo apt-get install -y -qq nginx openssl

# --- self-signed cert (IP in SAN so the browser accepts it after one warning) ---
sudo mkdir -p "$CERT_DIR"
if [ ! -f "$CERT_DIR/cert.pem" ]; then
    sudo openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
        -keyout "$CERT_DIR/key.pem" -out "$CERT_DIR/cert.pem" \
        -subj "/CN=zen-dashboard" -addext "subjectAltName=IP:${IP}"
    sudo chmod 600 "$CERT_DIR/key.pem"
fi

# nginx (www-data) must be able to traverse into the paper dir
chmod o+x /home/ubuntu

# --- locked-down site: only the dashboard + the 4 data files ---
sudo tee /etc/nginx/sites-available/zen >/dev/null <<'NGINX'
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name _;

    ssl_certificate     /etc/nginx/zen/cert.pem;
    ssl_certificate_key /etc/nginx/zen/key.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    add_header Cache-Control "no-store" always;   # no caching: always fresh
    server_tokens off;

    location = / {
        root /home/ubuntu/paper/docs;
        try_files /index.html =404;
    }
    location = /index.html {
        root /home/ubuntu/paper/docs;
    }
    # whitelist exactly the files the dashboard fetches
    location ~ ^/results/(paper_trades\.csv|paper_state\.json|paper_trade\.log|token_status\.json)$ {
        root /home/ubuntu/paper;
        default_type text/plain;
    }
    location / { return 404; }
}
NGINX

sudo ln -sf /etc/nginx/sites-available/zen /etc/nginx/sites-enabled/zen
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx

# --- VM firewall: accept 443 ahead of Oracle's default REJECT ---
if ! sudo iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null; then
    sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
fi
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iptables-persistent 2>/dev/null || true
sudo netfilter-persistent save 2>/dev/null || sudo sh -c 'iptables-save > /etc/iptables/rules.v4'

echo "============================================================"
echo "nginx serving https://${IP}/  (self-signed - accept the warning once)"
echo "REMAINING STEP (Oracle console): add an ingress rule"
echo "  Networking > VCN > Security List > Add Ingress Rule:"
echo "  Source 0.0.0.0/0 , IP Protocol TCP , Destination Port 443"
echo "============================================================"
sudo systemctl is-active nginx
