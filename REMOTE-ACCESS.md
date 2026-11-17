# Remote access (use it from anywhere) — Tailscale

This lets you reach the CNC File Manager from home, your phone, anywhere —
securely, without exposing the machine to the public internet.

How it works: the File Manager keeps running on the **workshop PC** (it has to,
to talk to the controller). Tailscale is a free, private VPN that joins your
devices into one network, so your laptop/phone can reach the workshop PC from
anywhere as if you were standing in the shop. Nothing is open to the internet —
only your signed-in devices can connect.

**In the shop:** devices on the shop Wi-Fi reach it over the LAN as normal
(`http://<workshop-pc>:8097`) — they do NOT need Tailscale. Tailscale is only
for getting in from *outside* the shop.

---

## Step 1 — Workshop PC (the host — do this first)

1. Double-click **`INSTALL.bat`** and click "Yes". It sets up the app, opens the
   firewall (shop LAN + Tailscale), and installs the Tailscale client. Then
   double-click **`start.bat`** to run the File Manager.
2. Open the **Tailscale** app and sign in. Use one account to own the network —
   e.g. the business Google account (cnc@cnccut.melbourne). Every device you give
   access to signs into this same account.
3. In the Tailscale admin console (login.tailscale.com → **Machines**), find the
   workshop PC and:
   - **Rename it** to something clean like `cnc-workshop` (this becomes the URL,
     and avoids the underscore problem the Windows name has).
   - **Disable key expiry** for it. It's an always-on server — you don't want it
     dropping off the network every few months asking to re-login.

Tailscale installs as a service, so it auto-starts when the PC boots.

## Step 2 — Your devices (laptop, phone, Jake's laptop)

Install Tailscale and sign into the **same account**:
- Windows / Mac: https://tailscale.com/download
- iPhone / Android: "Tailscale" in the App Store / Play Store

That's it — each device joins the private network automatically.

## Step 3 — Open it from anywhere

1. Make sure Tailscale is toggled **on** on your device.
2. Go to **`http://cnc-workshop:8097`** (or whatever you named the workshop PC).

Works from home, mobile data, a hotel — anywhere, as long as Tailscale is on.

---

## Notes

- **Never** forward port 8097 on the shop router to the internet. This app has no
  password — Tailscale IS the security layer. Keep it that way.
- Free tier covers a small team. Simplest is to put every device under your one
  account; you can invite separate user logins later if you want.
- MagicDNS (on by default) is what makes the `http://cnc-workshop:8097` name work
  on every device, including phones.
