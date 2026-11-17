# Use the dashboard on your phone

The Mac hosts one dashboard session. A phone on the same Wi-Fi can use the same controls and typed assistant messages. The selected Mac, Pi or Lynx continues to run detection and picking.

## Set up phone access

1. Find the Mac's current Wi-Fi address in System Settings → Wi-Fi → Details → TCP/IP. Use the Mac's address, not the router or gateway address.
2. From `capture_and_detect`, run this command with that address:

   ```sh
   python phone_setup.py --ip 192.168.1.20
   ```

3. Run the dashboard command printed by the helper. It starts the offline demo. The Mac opens `http://localhost:8087`; the phone opens the printed Wi-Fi address, such as `http://192.168.1.20:8087`.
4. On the Mac, open **Settings** and find the phone pairing code. Enter that code in the phone's pairing page. The browser stays paired for 12 hours, or until the dashboard restarts.
5. Set your OpenAI API key on the **Mac**. The phone can then use the same configured assistant. The API key is never returned to the phone.

This setup uses ordinary HTTP on your trusted private Wi-Fi. Pairing controls access, but HTTP does not encrypt traffic. No certificate installation is needed. Voice recording is not part of this version.

## Reuse the setup

The helper stores the pairing code and nonsecret connection paths in `~/.config/cup-dashboard/phone`. Set `--directory /path/to/private/folder` to choose another private folder, or `--port 8088` to use another port. The folder must be owned by you with permissions `700`.

Running setup again reuses the pairing code and preserves existing API settings. If the Mac's Wi-Fi IP changes, run it again with the new address and use the updated launch command. Existing certificate/key files from an earlier setup are left untouched and are not used by the HTTP command.

When connecting to a real controller, retain `--lan-http`, the host, origin and pairing arguments, and use the existing gateway/device arguments in place of `--demo`. Phone access does not change the robot's mode, clear recovery records or connect to a gateway by itself. Picking still requires the existing preview and confirmation steps.

## If the phone cannot connect

- Keep the Mac awake and leave the dashboard running.
- Connect both devices to the same Wi-Fi. Guest networks may prevent devices from reaching each other.
- Use the printed `http://` address and port. `localhost` on the phone refers to the phone itself.
- If macOS asks about incoming connections, allow this dashboard on your trusted network.
- After a restart, open Settings on the Mac and pair the phone again.

Only the configured private IP address is accepted for phone HTTP access. The server checks the actual peer address, exact Host/Origin headers and command CSRF token; pairing protects state reads as well as commands. Do not forward this port to the public internet.
