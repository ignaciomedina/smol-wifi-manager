#!/usr/bin/env python3
"""
Smol WiFi Manager - A Wayland-native application to scan and display WiFi networks
without requiring root privileges.
"""

import threading
import time
import traceback

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('NM', '1.0')

# pylint: disable=wrong-import-position
from gi.repository import Gtk, Adw, GLib, NM, Gdk


class SmolWifiManagerWindow(Adw.ApplicationWindow):
    """Main window class for Smol WiFi Manager application."""
    def __init__(self, app):
        super().__init__(application=app, title="Smol WiFi Manager")
        self.set_default_size(530, 800)
        self.set_size_request(530, 800)  # Minimum width: 530, Minimum height: 800

        # NetworkManager client
        self.client = NM.Client.new(None)

        # Connect to realize signal to remove decorations and apply CSS after window is realized
        self.connect("realize", self._on_window_realize)

        # Try to set window as undecorated before showing
        self.set_decorated(False)

        # Main box
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_content(main_box)

        # Header bar
        header = Adw.HeaderBar()
        main_box.append(header)

        # Refresh button
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        self.refresh_btn.connect("clicked", self.on_refresh_clicked)
        header.pack_start(self.refresh_btn)

        # Status label
        self.status_label = Gtk.Label(label="Ready to scan...")
        self.status_label.add_css_class("subtitle")
        self.status_label.set_margin_top(12)
        self.status_label.set_margin_bottom(12)
        self.status_label.set_margin_start(12)
        self.status_label.set_margin_end(12)
        main_box.append(self.status_label)

        # Scrolled window for network list
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        main_box.append(scrolled)

        # List box for networks
        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.add_css_class("boxed-list")
        scrolled.set_child(self.list_box)

        # Add custom CSS for connected network styling
        # We'll add this after the window is realized
        self.css_provider = Gtk.CssProvider()
        css = """
        .connected-network {
            background-color: alpha(@accent_bg_color, 0.3);
        }
        """
        self.css_provider.load_from_data(css.encode('utf-8'))

        # Track networks by BSSID for efficient updates
        self.network_rows = {}  # Maps BSSID -> (row, access_point, icon_widget, details_box)

        # Track currently expanded row for accordion behavior
        self.currently_expanded_row = None

        # Store WiFi device for connection operations
        self.wifi_device = None

        # Initial scan after window is shown
        GLib.idle_add(lambda: self.scan_networks(is_manual=True))

    def _on_window_realize(self, _widget):
        """Remove window decorations and apply CSS after window is realized"""
        # Get the underlying native window
        native = self.get_native()
        if native:
            # Try to set decorated on native window
            if hasattr(native, 'set_decorated'):
                native.set_decorated(False)
            # On Wayland, try to use surface API if available
            if hasattr(native, 'get_surface'):
                surface = native.get_surface()
                if surface:
                    # Try to set as override-redirect or similar
                    pass

        # Apply CSS provider after window is realized
        try:
            display = Gdk.Display.get_default()
            if display:
                Gtk.StyleContext.add_provider_for_display(
                    display,
                    self.css_provider,
                    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
        except (GLib.Error, RuntimeError) as e:
            print(f"Error applying CSS: {e}")
        # Also try on the window itself
        self.set_decorated(False)

        # Force update
        self.queue_draw()

    def on_refresh_clicked(self, _button):
        """Handle refresh button click"""
        self.scan_networks(is_manual=True)

    def scan_networks(self, is_manual=False):
        """Scan for WiFi networks"""
        # If already scanning, skip (unless manual refresh)
        if not self.refresh_btn.get_sensitive() and not is_manual:
            return

        # Only disable button if not already disabled
        # This prevents issues during resize or other events
        if self.refresh_btn.get_sensitive():
            self.refresh_btn.set_sensitive(False)
        if is_manual:
            self.status_label.set_text("Scanning for networks...")

        # Clear the list for manual refresh to show immediate update
        if is_manual:
            # Clear existing list
            child = self.list_box.get_first_child()
            while child:
                next_child = child.get_next_sibling()
                self.list_box.remove(child)
                child = next_child
            self.network_rows.clear()

        # Run scan in a separate thread to avoid blocking UI
        thread = threading.Thread(target=self._scan_thread)
        thread.daemon = True
        thread.start()

    def _scan_thread(self):
        """Thread function to scan networks"""
        try:
            wifi_device = self._get_wifi_device()
            if not wifi_device:
                GLib.idle_add(self._update_status, "No WiFi device found")
                GLib.idle_add(self._enable_refresh)
                return

            # Store WiFi device for connection operations
            self.wifi_device = wifi_device

            # Request a scan (trigger scan, it may already be in progress)
            try:
                wifi_device.request_scan_async(None, None, None)
            except (GLib.Error, RuntimeError):
                # Scan might already be in progress, that's okay
                pass

            # Wait for scan to complete with retries
            max_wait = 5
            wait_time = 0.5
            iterations = int(max_wait / wait_time)

            time.sleep(1.0)
            for _ in range(iterations):
                time.sleep(wait_time)
                access_points = wifi_device.get_access_points()
                if access_points:
                    self._handle_scan_results(wifi_device, access_points)
                    return

            # Final attempt: process whatever we have
            access_points = wifi_device.get_access_points()
            self._handle_scan_results(wifi_device, access_points)

        except (GLib.Error, RuntimeError) as e:
            error_msg = f"Error: {str(e)}"
            print(f"Scan error: {error_msg}")
            print(traceback.format_exc())
            GLib.idle_add(self._update_status, error_msg)
            GLib.idle_add(self._enable_refresh)

    def _get_wifi_device(self):
        """Return the first WiFi device managed by NetworkManager, or None."""
        for device in self.client.get_devices():
            if device.get_device_type() == NM.DeviceType.WIFI:
                return device
        return None

    def _handle_scan_results(self, wifi_device, access_points):
        """Populate UI based on scan results or show appropriate status."""
        if access_points:
            active_ap = wifi_device.get_active_access_point()
            if active_ap:
                active_bssid_raw = active_ap.get_bssid()
                if isinstance(active_bssid_raw, bytes):
                    active_bssid = ":".join([f"{b:02x}" for b in active_bssid_raw])
                else:
                    active_bssid = str(active_bssid_raw)
            else:
                active_bssid = None

            sorted_aps = sorted(access_points, key=lambda ap: ap.get_strength(), reverse=True)
            GLib.idle_add(self._populate_networks, sorted_aps, active_bssid)
        else:
            GLib.idle_add(self._update_status, "No networks found")
            GLib.idle_add(self._enable_refresh)

    def _populate_networks(self, access_points, active_bssid=None):
        """Update the list with network information without clearing it"""
        # Always re-enable refresh button when populating
        self.refresh_btn.set_sensitive(True)

        if not access_points:
            # If we have existing networks, keep them. Only show "No networks" if list is empty
            if len(self.network_rows) == 0:
                self.status_label.set_text("No networks found")
            return

        # Create a set of current BSSIDs (normalized to strings)
        current_bssids = {self._bssid_to_str(ap.get_bssid()) for ap in access_points}

        # Remove networks that are no longer available
        self._remove_missing_rows(current_bssids)

        # Sort access points by signal strength
        sorted_aps = sorted(access_points, key=lambda ap: ap.get_strength(), reverse=True)

        # Update or add networks in sorted order
        self._apply_rows(sorted_aps, active_bssid)

        # Re-sort the list by signal strength (simple approach)
        self._resort_list_simple(sorted_aps)

        self.status_label.set_text(f"Found {len(access_points)} networks")
        # Refresh button is already enabled in _populate_networks

    def _resort_list_simple(self, sorted_access_points):
        """Re-sort the list by signal strength - only resort if order changed significantly"""
        # Build list of rows in correct order
        ordered_rows = self._compute_ordered_rows(sorted_access_points)

        # Get current order
        current_rows = self._get_current_rows()

        # Check if reordering is needed
        if len(ordered_rows) == len(current_rows) and all(
            current_rows[i] == row for i, row in enumerate(ordered_rows)
        ):
            return

        # Reorder rows
        self._reorder_rows(current_rows, ordered_rows)

    def _bssid_to_str(self, bssid_raw):
            if isinstance(bssid_raw, bytes):
            return ":".join([f"{b:02x}" for b in bssid_raw])
        return str(bssid_raw)

    def _remove_missing_rows(self, current_bssids):
        bssids_to_remove = [b for b in self.network_rows if b not in current_bssids]
        for bssid in bssids_to_remove:
            row, _, _icon, _ = self.network_rows[bssid]
            self.list_box.remove(row)
            del self.network_rows[bssid]

    def _apply_rows(self, sorted_aps, active_bssid):
        for ap in sorted_aps:
            bssid = self._bssid_to_str(ap.get_bssid())
            is_active = bool(active_bssid and bssid == active_bssid)
            if bssid in self.network_rows:
                row, _old_ap, old_icon, details_box = self.network_rows[bssid]
                if details_box is None:
                    details_box = self._setup_expander_content(row, ap, is_active)
                    row.connect("notify::expanded", self._on_row_expanded, row)
                    new_icon = self._create_signal_icon(ap.get_strength())
                    if old_icon and old_icon.get_parent():
                        row.remove_suffix(old_icon)
                    row.add_suffix(new_icon)
                    self.network_rows[bssid] = (row, ap, new_icon, details_box)
                else:
                    new_icon = self._update_network_row(row, ap, is_active, old_icon)
                    self._update_expander_content(details_box, ap, is_active)
                    self.network_rows[bssid] = (row, ap, new_icon, details_box)
            else:
                row = Adw.ExpanderRow()
                icon = self._update_network_row(row, ap, is_active, None)
                details_box = self._setup_expander_content(row, ap, is_active)
                row.connect("notify::expanded", self._on_row_expanded, row)
                self.network_rows[bssid] = (row, ap, icon, details_box)
                self.list_box.append(row)

    def _compute_ordered_rows(self, sorted_access_points):
        ordered = []
        for ap in sorted_access_points:
            bssid = ap.get_bssid()
            if bssid in self.network_rows:
                row, _, _, _ = self.network_rows[bssid]
                ordered.append(row)
        return ordered

    def _get_current_rows(self):
        rows = []
        child = self.list_box.get_first_child()
        while child:
            rows.append(child)
            child = child.get_next_sibling()
        return rows

    def _reorder_rows(self, current_rows, ordered_rows):
        for i, target_row in enumerate(ordered_rows):
            try:
                current_index = current_rows.index(target_row)
            except ValueError:
                continue
            if current_index == i:
                continue
                if i > 0:
                    reference_row = ordered_rows[i - 1]
                        self.list_box.remove(target_row)
                        self.list_box.insert_after(target_row, reference_row)
            else:
                    self.list_box.remove(target_row)
                    self.list_box.prepend(target_row)
                    current_rows.remove(target_row)
            current_rows.insert(i, target_row)

    def _handle_failed_start(self, ssid_str, connection_result):
        msg = f"[CONNECT DEBUG] Connection failed to start: {connection_result}"
        print(msg)
        result_type = type(connection_result)
        msg = "[CONNECT DEBUG] Connection result type"
        print(f"{msg}: {result_type}")
        if self.wifi_device:
            device_state = self.wifi_device.get_state()
            msg = "[CONNECT DEBUG] Device state when connection failed"
            print(f"{msg}: {device_state}")
            active_conn = self.wifi_device.get_active_connection()
            msg = "[CONNECT DEBUG] Active connection when failed"
            print(f"{msg}: {active_conn}")
        error_text = f"Failed to start connection to {ssid_str}. Password may be required."
        GLib.idle_add(self.status_label.set_text, error_text)
        GLib.idle_add(self._enable_refresh)

    def _extract_active_connection(self, connection_result):
        if isinstance(connection_result, tuple):
            conn, active_conn = connection_result
            print(f"[CONNECT DEBUG] Connection object: {conn}")
            msg = "[CONNECT DEBUG] Active connection object"
            print(f"{msg}: {active_conn}")
            return active_conn
        msg = "[CONNECT DEBUG] Connection result is ActiveConnection"
        print(f"{msg}: {connection_result}")
        return connection_result

    def _log_active_connection(self, active_conn):
        has_get_id = hasattr(active_conn, 'get_id')
        conn_id = active_conn.get_id() if has_get_id else 'N/A'
        print(f"[CONNECT DEBUG] Active connection ID: {conn_id}")
        has_get_state = hasattr(active_conn, 'get_state')
        conn_state = active_conn.get_state() if has_get_state else 'N/A'
        msg = "[CONNECT DEBUG] Active connection state"
        print(f"{msg}: {conn_state}")
        try:
            if hasattr(active_conn, 'get_state'):
                conn_state = active_conn.get_state()
                msg = "[CONNECT DEBUG] ActiveConnection state"
                print(f"{msg}: {conn_state}")
                if hasattr(active_conn, 'get_error'):
                    error = active_conn.get_error()
                    if error:
                        msg = "[CONNECT DEBUG] ActiveConnection error"
                        print(f"{msg}: {error}")
        except (AttributeError, TypeError, GLib.Error) as e:
            msg = f"[CONNECT DEBUG] Could not get ActiveConnection details: {e}"
            print(msg)

    def _log_device_state_after_connection(self):
        if self.wifi_device:
            device_state = self.wifi_device.get_state()
            msg = "[CONNECT DEBUG] Device state after connection"
            print(f"{msg}: {device_state}")
            if device_state == NM.DeviceState.FAILED:
                print("[CONNECT DEBUG] WARNING: Device state is FAILED!")
                try:
                    active_conn_obj = self.wifi_device.get_active_connection()
                    if active_conn_obj:
                        msg = "[CONNECT DEBUG] Active connection exists"
                        print(f"{msg}: {active_conn_obj}")
                        if hasattr(active_conn_obj, 'get_state'):
                            state = active_conn_obj.get_state()
                            msg = "[CONNECT DEBUG] Active connection state"
                            print(f"{msg}: {state}")
                except (AttributeError, TypeError, GLib.Error) as e:
                    msg = "[CONNECT DEBUG] Error checking active connection"
                    print(f"{msg}: {e}")
            active_ap = self.wifi_device.get_active_access_point()
            print(f"[CONNECT DEBUG] Active access point: {active_ap}")

    def _find_existing_connection_by_ssid(self, ssid_str):
        """Return an existing NM.Connection for the given SSID, else None."""
        for conn in self.client.get_connections():
            try:
                wifi_setting = conn.get_setting_wireless()
                if not wifi_setting:
                    continue
                existing_ssid = wifi_setting.get_ssid()
                if not existing_ssid:
                    continue
                existing_ssid_str = NM.utils_ssid_to_utf8(existing_ssid.get_data())
                if existing_ssid_str == ssid_str:
                    return conn
            except (AttributeError, TypeError, GLib.Error):
                continue
        return None

    def _build_new_connection(self, access_point, ssid, password):
        """Create and return a configured NM.SimpleConnection for the AP."""
        connection = NM.SimpleConnection.new()
        wifi_setting = NM.SettingWireless.new()
        if hasattr(ssid, 'get_data'):
            ssid_glib_bytes = ssid
        elif isinstance(ssid, bytes):
            ssid_glib_bytes = GLib.Bytes.new(ssid)
        else:
            ssid_glib_bytes = GLib.Bytes.new(bytes(ssid))
        wifi_setting.set_property(NM.SETTING_WIRELESS_SSID, ssid_glib_bytes)
        wifi_setting.set_property(NM.SETTING_WIRELESS_MODE, NM.SETTING_WIRELESS_MODE_INFRA)

        flags = access_point.get_wpa_flags() | access_point.get_rsn_flags()
        ap_security_flags = getattr(NM, '80211ApSecurityFlags')
        if flags & (ap_security_flags.KEY_MGMT_PSK | ap_security_flags.KEY_MGMT_802_1X):
            security_setting = NM.SettingWirelessSecurity.new()
            if flags & (ap_security_flags.PAIR_CCMP | ap_security_flags.GROUP_CCMP):
                security_setting.set_property(NM.SETTING_WIRELESS_SECURITY_KEY_MGMT, "wpa-psk")
                security_setting.set_property(NM.SETTING_WIRELESS_SECURITY_PROTO, ["rsn"])
            elif flags & (ap_security_flags.PAIR_TKIP | ap_security_flags.GROUP_TKIP):
                security_setting.set_property(NM.SETTING_WIRELESS_SECURITY_KEY_MGMT, "wpa-psk")
                security_setting.set_property(NM.SETTING_WIRELESS_SECURITY_PROTO, ["wpa"])
            if password:
                security_setting.set_property(NM.SETTING_WIRELESS_SECURITY_PSK, password)
                print("[CONNECT DEBUG] Password provided for connection")
            connection.add_setting(wifi_setting)
            connection.add_setting(security_setting)
        else:
            connection.add_setting(wifi_setting)
        return connection

    def _create_network_row(self, access_point, is_active=False):
        """Create a row widget for a network"""
        row = Adw.ExpanderRow()
        _icon = self._update_network_row(row, access_point, is_active, None)
        _details_box = self._setup_expander_content(row, access_point, is_active)
        # Connect to expanded signal for accordion behavior
        row.connect("notify::expanded", self._on_row_expanded, row)
        # Store details_box will be handled by caller
        return row

    def _on_row_expanded(self, row, _param, _user_data):
        """Handle row expansion - collapse other rows for accordion behavior"""
        if row.get_expanded():
            # If this row is being expanded, collapse the previously expanded row
            if self.currently_expanded_row and self.currently_expanded_row != row:
                self.currently_expanded_row.set_expanded(False)
            self.currently_expanded_row = row
        else:
            # If this row is being collapsed, clear the reference
            if self.currently_expanded_row == row:
                self.currently_expanded_row = None

    def _create_signal_icon(self, strength):
        """Create a signal strength icon"""
        if strength >= 75:
            icon_name = "network-wireless-signal-excellent-symbolic"
        elif strength >= 50:
            icon_name = "network-wireless-signal-good-symbolic"
        elif strength >= 25:
            icon_name = "network-wireless-signal-ok-symbolic"
        else:
            icon_name = "network-wireless-signal-weak-symbolic"

        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(24)
        return icon

    def _update_network_row(self, row, access_point, is_active=False, old_icon=None):
        """Update an existing row with new network information"""
        # SSID
        ssid = access_point.get_ssid()
        if ssid:
            ssid_str = NM.utils_ssid_to_utf8(ssid.get_data())
        else:
            ssid_str = "Hidden Network"

        # Add "Connected" indicator if this is the active network
        if is_active:
            # Create title with "Connected" suffix
            title_text = f"{ssid_str} • Connected"
            row.set_title(title_text)
            # Highlight the active network with custom styling
            if "connected-network" not in list(row.get_css_classes()):
                row.add_css_class("connected-network")
        else:
            row.set_title(ssid_str)
            # Remove connected-network class if it was active before
            if "connected-network" in list(row.get_css_classes()):
                row.remove_css_class("connected-network")

        # Signal strength
        strength = access_point.get_strength()
        strength_str = f"{strength}%"

        # Security info
        flags = access_point.get_wpa_flags() | access_point.get_rsn_flags()
        security = []
        # Use getattr because attribute name starts with a number
        ap_security_flags = getattr(NM, '80211ApSecurityFlags')
        if flags & ap_security_flags.PAIR_WEP40:
            security.append("WEP")
        if flags & (ap_security_flags.PAIR_CCMP | ap_security_flags.GROUP_CCMP):
            security.append("WPA2")
        if flags & (ap_security_flags.PAIR_TKIP | ap_security_flags.GROUP_TKIP):
            security.append("WPA")
        if flags & ap_security_flags.KEY_MGMT_PSK:
            security.append("PSK")

        security_str = ", ".join(security) if security else "Open"

        # Frequency
        freq = access_point.get_frequency()
        freq_mhz = f"{freq / 1000:.0f} MHz" if freq > 0 else "N/A"

        # Subtitle with details
        subtitle = f"{strength_str} • {freq_mhz} • {security_str}"
        row.set_subtitle(subtitle)

        # Update signal strength icon
        new_icon = self._create_signal_icon(strength)

        # Remove old icon if it exists
        # Note: Adw.ExpanderRow doesn't have remove_suffix, so we use the parent container
        if old_icon:
            parent = old_icon.get_parent()
            if parent:
                # Remove from parent container
                parent.remove(old_icon)

        # Add new icon
        row.add_suffix(new_icon)

        return new_icon

    def _setup_expander_content(self, row, access_point, is_active):
        """Set up the expandable content for a network row"""
        # Create details box
        details_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        details_box.set_margin_top(12)
        details_box.set_margin_bottom(12)
        details_box.set_margin_start(12)
        details_box.set_margin_end(12)

        # Network details
        details_grid = Gtk.Grid()
        details_grid.set_column_spacing(12)
        details_grid.set_row_spacing(6)
        details_grid.set_column_homogeneous(False)

        # BSSID
        bssid = access_point.get_bssid()
        if bssid:
            # BSSID can be bytes or string depending on NetworkManager version
            if isinstance(bssid, bytes):
                bssid_str = ":".join([f"{b:02x}" for b in bssid])
            else:
                # Already a string, use as-is or format if needed
                bssid_str = str(bssid)
            self._add_detail_row(details_grid, "BSSID:", bssid_str, 0)

        # Channel
        freq = access_point.get_frequency()
        if freq > 0:
            channel = NM.utils_wifi_freq_to_channel(freq)
            channel_str = f"{channel} ({freq / 1000:.0f} MHz)"
            self._add_detail_row(details_grid, "Channel:", channel_str, 1)

        # Mode
        mode = access_point.get_mode()
        mode_str = str(mode).rsplit('.', maxsplit=1)[-1] if mode else "Unknown"
        self._add_detail_row(details_grid, "Mode:", mode_str, 2)

        # Max Bitrate
        bitrate = access_point.get_max_bitrate()
        if bitrate > 0:
            bitrate_str = f"{bitrate / 1000:.0f} Mbps"
            self._add_detail_row(details_grid, "Max Bitrate:", bitrate_str, 3)

        details_box.append(details_grid)

        # Separator
        separator = Gtk.Separator()
        details_box.append(separator)

        # Check if network requires password and has no saved connection
        flags = access_point.get_wpa_flags() | access_point.get_rsn_flags()
        ap_security_flags = getattr(NM, '80211ApSecurityFlags')
        requires_password = bool(
            flags & (ap_security_flags.KEY_MGMT_PSK | ap_security_flags.KEY_MGMT_802_1X)
        )

        # Check if there's an existing saved connection
        has_saved_connection = (
            self._has_saved_connection(access_point) if requires_password else False
        )

        # Connect/Disconnect button box
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        button_box.set_halign(Gtk.Align.FILL)
        button_box.set_margin_top(6)

        if is_active:
            spacer = Gtk.Box()
            spacer.set_hexpand(True)
            button_box.append(spacer)
            disconnect_btn = Gtk.Button(label="Disconnect")
            disconnect_btn.add_css_class("destructive-action")
            disconnect_btn.connect("clicked", self._on_disconnect_clicked, access_point)
            button_box.append(disconnect_btn)
        else:
            # If network requires password and has no saved connection, show password field
            if requires_password and not has_saved_connection:
                # Password entry
                password_entry = Gtk.PasswordEntry()
                password_entry.set_hexpand(True)
                # Store password entry in button_box for later retrieval
                button_box.append(password_entry)

                # Connect button
                connect_btn = Gtk.Button(label="Connect")
                connect_btn.add_css_class("suggested-action")
                # Disabled until password entered
                connect_btn.set_sensitive(False)
                connect_btn.connect(
                    "clicked", self._on_connect_clicked, access_point, password_entry
                )
                button_box.append(connect_btn)

                # Enable/disable button based on password length
                def on_password_changed(entry):
                    password = entry.get_text()
                    # Minimum 8 characters for WPA/WPA2
                    min_length = 8
                    connect_btn.set_sensitive(len(password) >= min_length)

                password_entry.connect("changed", on_password_changed)
            else:
                # No password needed or has saved connection
                connect_btn = Gtk.Button(label="Connect")
                connect_btn.add_css_class("suggested-action")
                connect_btn.connect("clicked", self._on_connect_clicked, access_point, None)
                button_box.append(connect_btn)

        details_box.append(button_box)

        row.add_row(details_box)
        return details_box

    def _update_expander_content(self, details_box, access_point, is_active):
        """Update the expander content when network status changes"""
        if not details_box:
            return

        # Find the button box (last child)
        children = []
        child = details_box.get_first_child()
        while child:
            children.append(child)
            child = child.get_next_sibling()

        # Remove old button box if exists (should be last)
        if len(children) > 1:
            old_button_box = children[-1]
            if isinstance(old_button_box, Gtk.Box):
                details_box.remove(old_button_box)

        # Check if network requires password and has no saved connection
        flags = access_point.get_wpa_flags() | access_point.get_rsn_flags()
        ap_security_flags = getattr(NM, '80211ApSecurityFlags')
        requires_password = bool(
            flags & (ap_security_flags.KEY_MGMT_PSK | ap_security_flags.KEY_MGMT_802_1X)
        )

        # Check if there's an existing saved connection
        has_saved_connection = (
            self._has_saved_connection(access_point) if requires_password else False
        )

        # Add new button box
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        button_box.set_halign(Gtk.Align.FILL)
        button_box.set_margin_top(6)

        if is_active:
            spacer = Gtk.Box()
            spacer.set_hexpand(True)
            button_box.append(spacer)
            disconnect_btn = Gtk.Button(label="Disconnect")
            disconnect_btn.add_css_class("destructive-action")
            disconnect_btn.connect("clicked", self._on_disconnect_clicked, access_point)
            button_box.append(disconnect_btn)
        else:
            # If network requires password and has no saved connection, show password
            if requires_password and not has_saved_connection:
                # Password entry
                password_entry = Gtk.PasswordEntry()
                password_entry.set_hexpand(True)
                button_box.append(password_entry)

                # Connect button
                connect_btn = Gtk.Button(label="Connect")
                connect_btn.add_css_class("suggested-action")
                # Disabled until password entered
                connect_btn.set_sensitive(False)
                connect_btn.connect(
                    "clicked", self._on_connect_clicked, access_point, password_entry
                )
                button_box.append(connect_btn)

                # Enable/disable button based on password length
                def on_password_changed(entry):
                    password = entry.get_text()
                    # Minimum 8 characters for WPA/WPA2
                    min_length = 8
                    connect_btn.set_sensitive(len(password) >= min_length)

                password_entry.connect("changed", on_password_changed)
            else:
                # No password needed or has saved connection
                connect_btn = Gtk.Button(label="Connect")
                connect_btn.add_css_class("suggested-action")
                connect_btn.connect("clicked", self._on_connect_clicked, access_point, None)
                button_box.append(connect_btn)

        details_box.append(button_box)

    def _add_detail_row(self, grid, label, value, row):
        """Add a detail row to the grid"""
        label_widget = Gtk.Label(label=label)
        label_widget.set_halign(Gtk.Align.START)
        label_widget.add_css_class("dim-label")

        value_widget = Gtk.Label(label=value)
        value_widget.set_halign(Gtk.Align.START)
        value_widget.set_hexpand(True)

        grid.attach(label_widget, 0, row, 1, 1)
        grid.attach(value_widget, 1, row, 1, 1)

    def _has_saved_connection(self, access_point):
        """Return True if there is a saved connection matching AP's SSID."""
        try:
            ssid = access_point.get_ssid()
            if not ssid:
                return False
            ssid_str = NM.utils_ssid_to_utf8(ssid.get_data())
            for conn in self.client.get_connections():
                wifi_setting = None
                try:
                    wifi_setting = conn.get_setting_wireless()
                except (AttributeError, TypeError, GLib.Error):
                    continue
                if not wifi_setting:
                    continue
                existing_ssid = wifi_setting.get_ssid()
                if not existing_ssid:
                    continue
                existing_ssid_str = NM.utils_ssid_to_utf8(existing_ssid.get_data())
                if existing_ssid_str == ssid_str:
                    return True
        except (AttributeError, TypeError, GLib.Error):
            return False
        return False

    def _on_connect_clicked(self, button, access_point, password_entry=None):
        """Handle connect button click"""
        if not self.wifi_device:
            self.status_label.set_text("Error: WiFi device not available")
            return

        # Get password if password entry is provided
        password = None
        if password_entry:
            password = password_entry.get_text()
            if len(password) < 8:
                self.status_label.set_text("Password must be at least 8 characters")
                return

        self.status_label.set_text("Connecting...")
        button.set_sensitive(False)

        # Run connection in a separate thread
        thread = threading.Thread(target=self._connect_thread, args=(access_point, password))
        thread.daemon = True
        thread.start()

    # pylint: disable=too-many-branches
    def _connect_thread(self, access_point, password=None):
        """Thread function to connect to a network"""
        try:
            # Get SSID
            ssid = access_point.get_ssid()
            if not ssid:
                msg = "Error: Network has no SSID"
                GLib.idle_add(self.status_label.set_text, msg)
                GLib.idle_add(self._enable_refresh)
                return

            ssid_str = NM.utils_ssid_to_utf8(ssid.get_data()) if ssid else "WiFi Network"

            # First, check if there's an existing saved connection for this SSID
            print(f"[CONNECT DEBUG] Looking for existing connection for SSID: {ssid_str}")
            existing_connection = self._find_existing_connection_by_ssid(ssid_str)

            if existing_connection:
                # Use existing connection - this should have the saved password
                conn_id = existing_connection.get_id()
                print(f"[CONNECT DEBUG] Using existing saved connection: {conn_id}")
                connection = existing_connection
                is_new_connection = False
            else:
                # No existing connection found, create a new one
                print("[CONNECT DEBUG] No existing connection found, creating new one")
                connection = self._build_new_connection(access_point, ssid, password)
                is_new_connection = True

            # Only create these settings if it's a new connection
            if is_new_connection:
                # Create connection setting
                conn_setting = NM.SettingConnection.new()
                conn_setting.set_property(NM.SETTING_CONNECTION_TYPE, "802-11-wireless")
                conn_setting.set_property(NM.SETTING_CONNECTION_ID, ssid_str)
                conn_setting.set_property(NM.SETTING_CONNECTION_UUID, NM.utils_uuid_generate())
                connection.add_setting(conn_setting)

                # Add IP4 config
                ip4_setting = NM.SettingIP4Config.new()
                ip4_setting.set_property("method", "auto")
                connection.add_setting(ip4_setting)

            # Validate connection before attempting (only for new connections)
            if is_new_connection:
                print(f"[CONNECT DEBUG] Validating new connection for {ssid_str}")
                if not connection.verify():
                    error_msg = "Connection validation failed"
                    print(f"[CONNECT DEBUG] Connection validation error: {error_msg}")
                    # Try to get more details about validation failure
                    try:
                        # NetworkManager might have more details
                        settings_names = [s.get_name() for s in connection.get_settings()]
                        print(f"[CONNECT DEBUG] Connection settings: {settings_names}")
                    except (AttributeError, TypeError, GLib.Error) as e:
                        print(f"[CONNECT DEBUG] Could not get connection details: {e}")
                    GLib.idle_add(self.status_label.set_text, error_msg)
                    GLib.idle_add(self._enable_refresh)
                    return
                print("[CONNECT DEBUG] Connection validation passed")
            else:
                print("[CONNECT DEBUG] Using existing connection, skipping validation")

            # Define callback for async connect
            def connect_callback(client, result, _user_data):
                try:
                    print(f"[CONNECT DEBUG] Starting connection callback for {ssid_str}")
                    # Use appropriate finish method based on connection type
                    if not is_new_connection:
                        # For existing connections, use activate_connection_finish
                        connection_result = client.activate_connection_finish(result)
                        print("[CONNECT DEBUG] Used activate_connection_finish")
                    else:
                        # For new connections, use add_and_activate_connection_finish
                        finish_method = client.add_and_activate_connection_finish
                        connection_result = finish_method(result)
                        print("[CONNECT DEBUG] Used add_and_activate_connection_finish")
                    print(f"[CONNECT DEBUG] Connection result type: {type(connection_result)}")
                    print(f"[CONNECT DEBUG] Connection result value: {connection_result}")

                    self._process_connection_result(connection_result, ssid_str)
                except (GLib.Error, RuntimeError) as e:
                    error_msg = f"Connection error: {str(e)}"
                    print(f"[CONNECT DEBUG] Exception in connect callback: {error_msg}")
                    print("[CONNECT DEBUG] Full traceback:")
                    print(traceback.format_exc())
                    if self.wifi_device:
                        try:
                            device_state = self.wifi_device.get_state()
                            msg = "[CONNECT DEBUG] Device state during exception"
                            print(f"{msg}: {device_state}")
                        except (AttributeError, GLib.Error):
                            pass
                    GLib.idle_add(self.status_label.set_text, f"Connection error: {error_msg}")
                    GLib.idle_add(self._enable_refresh)

            # Connect asynchronously
            # Note: This must be called from main thread, so we use GLib.idle_add
            def do_connect():
                try:
                    print(f"[CONNECT DEBUG] Starting async connection to {ssid_str}")
                    conn_type = 'existing' if not is_new_connection else 'new'
                    print(f"[CONNECT DEBUG] Using {conn_type} connection")
                    print(f"[CONNECT DEBUG] Connection object: {connection}")
                    has_get_id = hasattr(connection, 'get_id')
                    conn_id = connection.get_id() if has_get_id else 'N/A'
                    print(f"[CONNECT DEBUG] Connection ID: {conn_id}")
                    print(f"[CONNECT DEBUG] WiFi device: {self.wifi_device}")
                    if self.wifi_device:
                        state = self.wifi_device.get_state()
                        print(f"[CONNECT DEBUG] Device state before connect: {state}")
                        device_type = self.wifi_device.get_device_type()
                        print(f"[CONNECT DEBUG] Device type: {device_type}")
                    self.status_label.set_text(f"Connecting to {ssid_str}...")

                    # For existing connections, we can use activate_connection_async
                    # For new connections, use add_and_activate_connection_async
                    if not is_new_connection:
                        # Activate existing connection
                        print("[CONNECT DEBUG] Activating existing connection")
                        self.client.activate_connection_async(
                            connection,
                            self.wifi_device,
                            None,  # specific_object
                            None,  # cancellable
                            connect_callback,
                            None   # user_data
                        )
                    else:
                        # Add and activate new connection
                        print("[CONNECT DEBUG] Adding and activating new connection")
                        self.client.add_and_activate_connection_async(
                            connection,
                            self.wifi_device,
                            None,  # specific_object
                            None,  # cancellable
                            connect_callback,
                            None   # user_data
                        )
                    print("[CONNECT DEBUG] Async connection request sent")
                except (GLib.Error, RuntimeError) as e:
                    error_msg = f"Failed to start connection: {str(e)}"
                    print(f"[CONNECT DEBUG] Exception starting async connection: {error_msg}")
                    print("[CONNECT DEBUG] Full traceback:")
                    print(traceback.format_exc())
                    self.status_label.set_text(error_msg)
                    self._enable_refresh()
                return False

            # Run connection in main thread
            GLib.idle_add(do_connect)

        except (GLib.Error, RuntimeError) as e:
            error_msg = f"Connection error: {str(e)}"
            print(f"Connect thread error: {error_msg}")
            print(traceback.format_exc())
            GLib.idle_add(self.status_label.set_text, error_msg)
            GLib.idle_add(self._enable_refresh)

    def _refresh_after_connect(self):
        """Refresh the network list after connect to update UI"""
        # Reload the list using manual refresh
        if self.refresh_btn.get_sensitive():
            self.scan_networks(is_manual=True)
        return False  # Don't repeat

    def _on_disconnect_clicked(self, button, _access_point):
        """Handle disconnect button click"""
        if not self.wifi_device:
            self.status_label.set_text("Error: WiFi device not available")
            return

        self.status_label.set_text("Disconnecting...")
        button.set_sensitive(False)

        # Run disconnection in a separate thread
        thread = threading.Thread(target=self._disconnect_thread)
        thread.daemon = True
        thread.start()

    def _disconnect_thread(self):
        """Thread function to disconnect from network"""
        try:
            # Get active connection from the WiFi device
            active_connection = self.wifi_device.get_active_connection()

            if not active_connection:
                # Try getting from client instead
                active_connections = self.client.get_active_connections()
                active_connection = None
                for conn in active_connections:
                    devices = conn.get_devices()
                    for device in devices:
                        if device == self.wifi_device:
                            active_connection = conn
                            break
                    if active_connection:
                        break

            if active_connection:
                # Define callback for async disconnect
                def disconnect_callback(client, result, _user_data):
                    try:
                        client.deactivate_connection_finish(result)
                        GLib.idle_add(self.status_label.set_text, "Disconnected")
                        # Wait 2 seconds after disconnect, then reload the list
                        GLib.timeout_add_seconds(2, self._refresh_after_disconnect)
                    except (GLib.Error, RuntimeError) as e:
                        error_msg = f"Disconnect error: {str(e)}"
                        print(f"Disconnect callback error: {error_msg}")
                        GLib.idle_add(self.status_label.set_text, error_msg)
                        GLib.idle_add(self._enable_refresh)

                # Disconnect asynchronously
                # Note: deactivate_connection_async signature is:
                # (connection, cancellable, callback, user_data)
                self.client.deactivate_connection_async(
                    active_connection,
                    None,  # cancellable
                    disconnect_callback,
                    None   # user_data
                )
                GLib.idle_add(self.status_label.set_text, "Disconnecting...")
            else:
                GLib.idle_add(self.status_label.set_text, "No active connection to disconnect")
                GLib.idle_add(self._enable_refresh)

        except (GLib.Error, RuntimeError) as e:
            error_msg = f"Disconnect error: {str(e)}"
            print(f"Disconnect thread error: {error_msg}")
            print(traceback.format_exc())
            GLib.idle_add(self.status_label.set_text, error_msg)
            GLib.idle_add(self._enable_refresh)

    def _update_status(self, message):
        """Update status label"""
        self.status_label.set_text(message)

    def _refresh_after_disconnect(self):
        """Refresh the network list after disconnect to update UI"""
        # Reload the list using manual refresh (clears and reloads)
        if self.refresh_btn.get_sensitive():
            self.scan_networks(is_manual=True)
        return False  # Don't repeat

    def _enable_refresh(self):
        """Re-enable refresh button"""
        # Use GLib.idle_add to ensure this happens in the main thread
        # and prevent issues during window resize
        GLib.idle_add(self._do_enable_refresh)

    def _do_enable_refresh(self):
        """Actually enable the refresh button (called from main thread)"""
        self.refresh_btn.set_sensitive(True)
        return False  # Don't repeat

    # pylint: disable=too-many-branches
    def _process_connection_result(self, connection_result, ssid_str):
        """Handle async connection result, update UI and schedule follow-up checks."""
        if not connection_result:
            self._handle_failed_start(ssid_str, connection_result)
            return

        active_conn = self._extract_active_connection(connection_result)
        if active_conn:
            self._log_active_connection(active_conn)

        self._log_device_state_after_connection()

        GLib.idle_add(self.status_label.set_text, f"Connecting to {ssid_str}...")

        # Wait for device to be fully connected before refreshing
        max_checks = 30  # Maximum 15 seconds (30 * 500ms)
        check_count = [0]

        # pylint: disable=too-many-branches
        def check_and_refresh():
            check_count[0] += 1
            if check_count[0] > max_checks:
                msg = (
                    "[CONNECT DEBUG] Connection timed out after "
                    f"{max_checks} checks"
                )
                print(msg)
                if self.wifi_device:
                    final_state = self.wifi_device.get_state()
                    msg = "[CONNECT DEBUG] Final device state"
                    print(f"{msg}: {final_state}")
                    active_conn_local = self.wifi_device.get_active_connection()
                    msg = "[CONNECT DEBUG] Final active connection"
                    print(f"{msg}: {active_conn_local}")
                self.status_label.set_text(f"Connection to {ssid_str} timed out")
                self._enable_refresh()
                return False

            if self.wifi_device:
                state = self.wifi_device.get_state()
                check_num = check_count[0]
                msg = f"[CONNECT DEBUG] Device state check #{check_num}"
                print(f"{msg}: {state}")

                active_ap = self.wifi_device.get_active_access_point()
                active_connection = self.wifi_device.get_active_connection()
                ap_msg = (
                    f"Active AP: {active_ap}, "
                    f"Active connection: {active_connection}"
                )
                print(f"[CONNECT DEBUG] {ap_msg}")

                if active_connection:
                    has_get_state = hasattr(active_connection, 'get_state')
                    ac_state = active_connection.get_state() if has_get_state else 'N/A'
                    print(f"[CONNECT DEBUG] Active connection state: {ac_state}")
                    has_get_id = hasattr(active_connection, 'get_id')
                    ac_id = active_connection.get_id() if has_get_id else 'N/A'
                    print(f"[CONNECT DEBUG] Active connection ID: {ac_id}")

                if state == NM.DeviceState.ACTIVATED:
                    self.status_label.set_text(f"Connected to {ssid_str}")
                    self._refresh_after_connect()
                    return False
                connecting_states = (
                    NM.DeviceState.IP_CONFIG,
                    NM.DeviceState.IP_CHECK,
                    NM.DeviceState.PREPARE,
                    NM.DeviceState.CONFIG,
                    NM.DeviceState.NEED_AUTH
                )
                if state in connecting_states:
                    GLib.timeout_add(500, check_and_refresh)
                    return False
                if state == NM.DeviceState.DISCONNECTED:
                    print(
                        "[CONNECT DEBUG] Device is DISCONNECTED "
                        f"(check #{check_num})"
                    )
                    ac_exists = active_connection is not None
                    print(f"[CONNECT DEBUG] Active connection exists: {ac_exists}")
                    if active_connection:
                        msg = "[CONNECT DEBUG] Active connection details"
                        print(f"{msg}: {active_connection}")
                    if check_count[0] <= 3:
                        print("[CONNECT DEBUG] Still early, waiting...")
                        GLib.timeout_add(500, check_and_refresh)
                        return False
                    if active_connection and check_count[0] <= 6:
                        print(
                            "[CONNECT DEBUG] Active connection exists "
                            "but device disconnected"
                        )
                        GLib.timeout_add(500, check_and_refresh)
                        return False
                    msg = (
                        "[CONNECT DEBUG] Connection failed after "
                        f"{check_num} checks"
                    )
                    print(msg)
                    if active_connection:
                        msg = (
                            "[CONNECT DEBUG] Active connection object "
                            "but device never connected"
                        )
                        print(msg)
                    self.status_label.set_text(
                        f"Connection to {ssid_str} failed (disconnected)"
                    )
                    self._enable_refresh()
                    return False
                if state == NM.DeviceState.FAILED:
                    print("[CONNECT DEBUG] Device state is FAILED")
                    try:
                        active_conn_local = self.wifi_device.get_active_connection()
                        if active_conn_local and hasattr(active_conn_local, 'get_state'):
                            conn_state = active_conn_local.get_state()
                            msg = (
                                "[CONNECT DEBUG] ActiveConnection state when device failed"
                            )
                            print(f"{msg}: {conn_state}")
                        has_state = hasattr(active_connection, 'get_state')
                        if active_connection and has_state:
                            ac_state = active_connection.get_state()
                            print(f"[CONNECT DEBUG] ActiveConnection state: {ac_state}")
                    except (AttributeError, TypeError, GLib.Error) as e:
                        print(f"[CONNECT DEBUG] Error getting failure details: {e}")

                    self.status_label.set_text(
                        f"Connection to {ssid_str} failed. Password may be required."
                    )
                    self._enable_refresh()
                    return False
                if state in (NM.DeviceState.UNMANAGED, NM.DeviceState.UNAVAILABLE):
                    state_names = {
                        NM.DeviceState.UNMANAGED: "unmanaged",
                        NM.DeviceState.UNAVAILABLE: "unavailable"
                    }
                    state_name = state_names.get(state, "unknown")
                    self.status_label.set_text(
                        f"Connection to {ssid_str} failed ({state_name})"
                    )
                    self._enable_refresh()
                    return False
                if check_count[0] < 5:
                    GLib.timeout_add(500, check_and_refresh)
                    return False
                self.status_label.set_text(
                    f"Connection to {ssid_str} failed (state: {state})"
                )
                self._enable_refresh()
                return False

            self.status_label.set_text(f"Connected to {ssid_str}")
            self._refresh_after_connect()
            return False

        GLib.timeout_add_seconds(2, check_and_refresh)


class SmolWifiManagerApp(Adw.Application):
    """Main application class for Smol WiFi Manager."""

    def __init__(self):
        super().__init__(application_id="com.smol.wifimanager")
        self.win = None
        self.connect("activate", self.on_activate)

    def on_activate(self, _app):
        """Handle application activation"""
        self.win = SmolWifiManagerWindow(self)
        self.win.present()
        # Try to remove decorations after presenting
        GLib.idle_add(self._remove_decorations)

    def _remove_decorations(self):
        """Remove window decorations"""
        if hasattr(self, 'win') and self.win:
            self.win.set_decorated(False)
            # Try on native window too
            native = self.win.get_native()
            if native:
                native.set_decorated(False)
        return False  # Don't repeat


def main():
    """Main entry point for the application."""
    app = SmolWifiManagerApp()
    app.run(None)


if __name__ == "__main__":
    main()
