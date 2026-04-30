# Deploy to `video.ruohong.fun`

This app is easier to deploy on a dedicated subdomain than under a URL path.
Use `video.ruohong.fun` and proxy the whole site to the Flask app running on port `8080`.

## 1. DNS

Create an `A` record:

- Host: `video`
- Value: your cloud server public IPv4

If you also have IPv6, add an `AAAA` record for `video`.

## 2. Server bootstrap

These steps assume Ubuntu 22.04 or 24.04:

```bash
sudo apt update
sudo apt install -y python3 python3-venv nginx certbot python3-certbot-nginx
sudo mkdir -p /opt/video_generation
sudo chown "$USER":"$USER" /opt/video_generation
```

Copy the repo to `/opt/video_generation`, then install Python deps:

```bash
cd /opt/video_generation
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

The app also requires:

- `dreamina` CLI installed on the server
- a valid logged-in Dreamina session for the Linux user running the service

## 3. systemd service

Copy the service file:

```bash
sudo cp deploy/video_generation.service /etc/systemd/system/video_generation.service
```

Update these values in `/etc/systemd/system/video_generation.service` if needed:

- `User`
- `WorkingDirectory`
- `ExecStart`

Then start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now video_generation
sudo systemctl status video_generation
```

## 4. Nginx reverse proxy

Copy the Nginx site config:

```bash
sudo cp deploy/nginx.video.ruohong.fun.conf /etc/nginx/sites-available/video.ruohong.fun
sudo ln -sf /etc/nginx/sites-available/video.ruohong.fun /etc/nginx/sites-enabled/video.ruohong.fun
sudo nginx -t
sudo systemctl reload nginx
```

## 5. HTTPS

After DNS resolves to the server, issue the certificate:

```bash
sudo certbot --nginx -d video.ruohong.fun
```

## 6. Verify

Open:

- `https://video.ruohong.fun/`

Useful checks:

```bash
curl -I http://127.0.0.1:8080/
curl -I https://video.ruohong.fun/
sudo journalctl -u video_generation -f
```

## Notes

- This app currently exposes generation endpoints to anyone who can reach the site.
- Before making it public, strongly consider adding authentication or an IP allowlist, otherwise other people can consume your Dreamina credits.
- Uploaded files, tasks, and workflow state are stored locally in:
  - `uploads/`
  - `tasks.json`
  - `workflow.json`
