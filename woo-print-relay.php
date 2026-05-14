<?php
/**
 * Plugin Name: Print Relay — Restaurant Asia Shanghai
 * Description: Token für Druck-System — im Control Panel manuell hinzufügen
 * Version: 2.0
 */

if (!defined('ABSPATH')) exit;

define('PRINT_RELAY_SERVER', 'http://relay.thecarte.eu:51901');
define('PRINT_RELAY_OPTION', 'print_relay_token');

// ── Admin page ──
add_action('admin_menu', function() {
    add_options_page('Print Relay', 'Print Relay', 'manage_options', 'print-relay', 'print_relay_page');
});

function print_relay_page() {
    $token = get_option(PRINT_RELAY_OPTION, '');

    // Generate token on first visit
    if (empty($token)) {
        $token = bin2hex(random_bytes(4)); // 8 chars
        update_option(PRINT_RELAY_OPTION, $token);
    }

    $name = get_bloginfo('name');
    ?>
    <div class="wrap">
        <h1>🖨️ Print Relay</h1>
        <p><?php echo esc_html($name); ?></p>
        <div style="background:#fff;border:1px solid #ccd0d4;border-radius:8px;padding:24px;max-width:500px;margin-top:16px">
            <p style="font-size:14px;color:#666">Pairing-Token:</p>
            <div style="font:bold 32px monospace;background:#e3f2fd;padding:14px 24px;border-radius:6px;letter-spacing:3px;display:inline-block;margin:8px 0;cursor:pointer"
                 onclick="navigator.clipboard.writeText('<?php echo esc_js($token); ?>');alert('Kopiert!')"
                 title="Klicken zum Kopieren">
                <?php echo esc_html($token); ?>
            </div>
            <p style="margin-top:16px;color:#555;font-size:13px">
                Kopieren Sie diesen Code und fügen Sie ihn im<br>
                <strong>Print Relay Control Panel</strong> unter "➕ Pairing hinzufügen" ein.
            </p>
            <p style="margin-top:8px;color:#999;font-size:11px">
                Control Panel: <code><?php echo esc_html(PRINT_RELAY_SERVER); ?></code>
            </p>
            <hr style="margin:16px 0;border:0;border-top:1px solid #eee">
            <p style="color:#888;font-size:12px">
                <strong>Webhook-URL</strong> (nach Pairing im Panel einrichten):<br>
                <code style="background:#f5f5f5;padding:2px 6px;border-radius:3px;font-size:11px">
                    <?php echo esc_html(PRINT_RELAY_SERVER); ?>/wc?token=<?php echo esc_html($token); ?>
                </code>
            </p>
        </div>
        <p style="margin-top:12px;color:#888;font-size:11px">
            ⚠️ Keine automatische Registrierung — Token muss manuell im Control Panel hinzugefügt werden.
        </p>
    </div>
    <?php
}
