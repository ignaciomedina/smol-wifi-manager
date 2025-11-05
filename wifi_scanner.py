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
        except Exception as e:
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
            # Get all devices
            devices = self.client.get_devices()
            wifi_device = None

            # Find WiFi device
            for device in devices:
                if device.get_device_type() == NM.DeviceType.WIFI:
                    wifi_device = device
                    break

            if not wifi_device:
                GLib.idle_add(self._update_status, "No WiFi device found")
                GLib.idle_add(self._enable_refresh)
                return

            # Store WiFi device for connection operations
            self.wifi_device = wifi_device

            # Request a scan (trigger scan, it may already be in progress)
            try:
                wifi_device.request_scan_async(None, None, None)
            except Exception:
                pass  # Scan might already be in progress, that's okay

            # Wait for scan to complete with retries
            # NetworkManager scans are typically fast, but we'll wait up to 5 seconds
            max_wait = 5
            wait_time = 0.5
            iterations = int(max_wait / wait_time)

            # Wait a bit for scan to start
            time.sleep(1.0)

            for _ in range(iterations):
                time.sleep(wait_time)
                access_points = wifi_device.get_access_points()

                # If we have access points, process them
                # Wait at least 1 second after requesting scan
                if access_points and len(access_points) > 0:
                    # Get active access point
                    active_ap = wifi_device.get_active_access_point()
                    if active_ap:
                        active_bssid_raw = active_ap.get_bssid()
                        # Normalize BSSID to string for comparison
                        if isinstance(active_bssid_raw, bytes):
                            active_bssid = ":".join([f"{b:02x}" for b in active_bssid_raw])
                        else:
                            active_bssid = str(active_bssid_raw)
                    else:
                        active_bssid = None

                    # Sort by signal strength (descending)
                    sorted_aps = sorted(
                        access_points,
                        key=lambda ap: ap.get_strength(),
                        reverse=True
                    )

                    # Update UI in main thread
                    GLib.idle_add(self._populate_networks, sorted_aps, active_bssid)
                    return

            # Final attempt: get access points even if scan didn't complete
            access_points = wifi_device.get_access_points()
            if access_points and len(access_points) > 0:
                # Get active access point
                active_ap = wifi_device.get_active_access_point()
                active_bssid = active_ap.get_bssid() if active_ap else None

                sorted_aps = sorted(access_points, key=lambda ap: ap.get_strength(), reverse=True)
                GLib.idle_add(self._populate_networks, sorted_aps, active_bssid)
            else:
                GLib.idle_add(self._update_status, "No networks found")
                GLib.idle_add(self._enable_refresh)

        except Exception as e:
            error_msg = f"Error: {str(e)}"
            print(f"Scan error: {error_msg}")
            print(traceback.format_exc())
            GLib.idle_add(self._update_status, error_msg)
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
        current_bssids = set()
        for ap in access_points:
            bssid_raw = ap.get_bssid()
            if isinstance(bssid_raw, bytes):
                bssid_str = ":".join([f"{b:02x}" for b in bssid_raw])
            else:
                bssid_str = str(bssid_raw)
            current_bssids.add(bssid_str)

        # Remove networks that are no longer available
        bssids_to_remove = []
        for bssid in self.network_rows:
            if bssid not in current_bssids:
                bssids_to_remove.append(bssid)

        for bssid in bssids_to_remove:
            row, _, icon, _ = self.network_rows[bssid]
            self.list_box.remove(row)
            del self.network_rows[bssid]

        # Sort access points by signal strength
        sorted_aps = sorted(access_points, key=lambda ap: ap.get_strength(), reverse=True)

        # Update or add networks in sorted order
        for ap in sorted_aps:
            bssid_raw = ap.get_bssid()
            # Normalize BSSID to string for comparison
            if isinstance(bssid_raw, bytes):
                bssid = ":".join([f"{b:02x}" for b in bssid_raw])
            else:
                bssid = str(bssid_raw)
            is_active = active_bssid and bssid == active_bssid

            if bssid in self.network_rows:
                # Update existing row
                row, _old_ap, old_icon, details_box = self.network_rows[bssid]
                # Ensure expander content is set up if it wasn't before
                if details_box is None:
                    details_box = self._setup_expander_content(row, ap, is_active)
                    # Connect to expanded signal for accordion behavior
                    row.connect("notify::expanded", self._on_row_expanded, row)
                    # Create icon for new expander
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
                # Create new row
                row = Adw.ExpanderRow()
                icon = self._update_network_row(row, ap, is_active, None)
                details_box = self._setup_expander_content(row, ap, is_active)
                # Connect to expanded signal for accordion behavior
                row.connect("notify::expanded", self._on_row_expanded, row)
                self.network_rows[bssid] = (row, ap, icon, details_box)
                # Insert at the end for now, will be sorted below
                self.list_box.append(row)

        # Re-sort the list by signal strength (simple approach)
        self._resort_list_simple(sorted_aps)

        self.status_label.set_text(f"Found {len(access_points)} networks")
        # Refresh button is already enabled in _populate_networks

    def _resort_list_simple(self, sorted_access_points):
        """Re-sort the list by signal strength - only resort if order changed significantly"""
        # Build list of rows in correct order
        ordered_rows = []
        for ap in sorted_access_points:
            bssid = ap.get_bssid()
            if bssid in self.network_rows:
                row, _, _, _ = self.network_rows[bssid]
                ordered_rows.append(row)

        # Get current order
        current_rows = []
        child = self.list_box.get_first_child()
        while child:
            current_rows.append(child)
            child = child.get_next_sibling()

        # Check if reordering is needed
        needs_reorder = False
        if len(ordered_rows) != len(current_rows):
            needs_reorder = True
        else:
            for i, row in enumerate(ordered_rows):
                if current_rows[i] != row:
                    needs_reorder = True
                    break

        if not needs_reorder:
            return

        # Reorder by moving items in place (less flicker than remove/re-add)
        # Use a more efficient approach: move items one by one to correct position
        for i, target_row in enumerate(ordered_rows):
            current_index = -1
            for j, row in enumerate(current_rows):
                if row == target_row:
                    current_index = j
                    break

            if current_index != i and current_index >= 0:
                # Find reference row (row before target position)
                if i > 0:
                    reference_row = ordered_rows[i - 1]
                    # Check if reference is already before target
                    ref_index = -1
                    for j, row in enumerate(current_rows):
                        if row == reference_row:
                            ref_index = j
                            break
                    if ref_index != i - 1:
                        # Move target_row after reference_row
                        self.list_box.remove(target_row)
                        self.list_box.insert_after(target_row, reference_row)
                        # Update current_rows tracking
                        current_rows.remove(target_row)
                        current_rows.insert(i, target_row)
                elif i == 0:
                    # Move to top
                    self.list_box.remove(target_row)
                    self.list_box.prepend(target_row)
                    current_rows.remove(target_row)
                    current_rows.insert(0, target_row)

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
        has_saved_connection = False
        if requires_password:
            ssid = access_point.get_ssid()
            if ssid:
                ssid_str = NM.utils_ssid_to_utf8(ssid.get_data())
                all_connections = self.client.get_connections()
                for conn in all_connections:
                    try:
                        wifi_setting = conn.get_setting_wireless()
                        if wifi_setting:
                            existing_ssid = wifi_setting.get_ssid()
                            if existing_ssid:
                                existing_ssid_str = NM.utils_ssid_to_utf8(existing_ssid.get_data())
                                if existing_ssid_str == ssid_str:
                                    has_saved_connection = True
                                    break
                    except Exception:
                        continue

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
        has_saved_connection = False
        if requires_password:
            ssid = access_point.get_ssid()
            if ssid:
                ssid_str = NM.utils_ssid_to_utf8(ssid.get_data())
                all_connections = self.client.get_connections()
                for conn in all_connections:
                    try:
                        wifi_setting = conn.get_setting_wireless()
                        if wifi_setting:
                            existing_ssid = wifi_setting.get_ssid()
                            if existing_ssid:
                                existing_ssid_str = NM.utils_ssid_to_utf8(existing_ssid.get_data())
                                if existing_ssid_str == ssid_str:
                                    has_saved_connection = True
                                    break
                    except Exception:
                        continue

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
            # This is what nmcli does - it looks for existing connections first
            print(f"[CONNECT DEBUG] Looking for existing connection for SSID: {ssid_str}")
            existing_connection = None
            all_connections = self.client.get_connections()

            for conn in all_connections:
                try:
                    # Get the connection's WiFi setting
                    wifi_setting = conn.get_setting_wireless()
                    if wifi_setting:
                        # Get SSID from existing connection
                        existing_ssid = wifi_setting.get_ssid()
                        if existing_ssid:
                            existing_ssid_str = NM.utils_ssid_to_utf8(
                                existing_ssid.get_data()
                            )
                            if existing_ssid_str == ssid_str:
                                conn_id = conn.get_id()
                                print(f"[CONNECT DEBUG] Found existing connection: {conn_id}")
                                existing_connection = conn
                                break
                except Exception as e:
                    print(f"[CONNECT DEBUG] Error checking connection: {e}")
                    continue

            if existing_connection:
                # Use existing connection - this should have the saved password
                conn_id = existing_connection.get_id()
                print(f"[CONNECT DEBUG] Using existing saved connection: {conn_id}")
                connection = existing_connection
                is_new_connection = False
            else:
                # No existing connection found, create a new one
                print("[CONNECT DEBUG] No existing connection found, creating new one")
                connection = NM.SimpleConnection.new()
                is_new_connection = True

                # Only create WiFi settings for new connections
                # Create WiFi setting
                wifi_setting = NM.SettingWireless.new()
                # Set SSID - access_point.get_ssid() returns GLib.Bytes
                if hasattr(ssid, 'get_data'):
                    # It's already GLib.Bytes, use it directly
                    ssid_glib_bytes = ssid
                elif isinstance(ssid, bytes):
                    ssid_glib_bytes = GLib.Bytes.new(ssid)
                else:
                    # Convert to bytes first, then to GLib.Bytes
                    ssid_glib_bytes = GLib.Bytes.new(bytes(ssid))

                wifi_setting.set_property(NM.SETTING_WIRELESS_SSID, ssid_glib_bytes)
                wifi_setting.set_property(NM.SETTING_WIRELESS_MODE, NM.SETTING_WIRELESS_MODE_INFRA)

                # Create wireless security setting if needed
                flags = access_point.get_wpa_flags() | access_point.get_rsn_flags()
                ap_security_flags = getattr(NM, '80211ApSecurityFlags')

                if flags & (ap_security_flags.KEY_MGMT_PSK | ap_security_flags.KEY_MGMT_802_1X):
                    # Network requires password
                    security_setting = NM.SettingWirelessSecurity.new()

                    # Check if it's WPA or WPA2
                    if flags & (ap_security_flags.PAIR_CCMP | ap_security_flags.GROUP_CCMP):
                        key_mgmt = NM.SETTING_WIRELESS_SECURITY_KEY_MGMT
                        security_setting.set_property(key_mgmt, "wpa-psk")
                        proto = NM.SETTING_WIRELESS_SECURITY_PROTO
                        security_setting.set_property(proto, ["rsn"])
                    elif flags & (ap_security_flags.PAIR_TKIP | ap_security_flags.GROUP_TKIP):
                        key_mgmt = NM.SETTING_WIRELESS_SECURITY_KEY_MGMT
                        security_setting.set_property(key_mgmt, "wpa-psk")
                        proto = NM.SETTING_WIRELESS_SECURITY_PROTO
                        security_setting.set_property(proto, ["wpa"])

                    # Set password if provided
                    if password:
                        security_setting.set_property(NM.SETTING_WIRELESS_SECURITY_PSK, password)
                        print("[CONNECT DEBUG] Password provided for connection")

                    connection.add_setting(wifi_setting)
                    connection.add_setting(security_setting)
                else:
                    # Open network
                    connection.add_setting(wifi_setting)

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
                    except Exception as e:
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

                    if connection_result:
                        # Check what was returned - could be ActiveConnection or tuple
                        active_conn = None
                        if isinstance(connection_result, tuple):
                            conn, active_conn = connection_result
                            print(f"[CONNECT DEBUG] Connection object: {conn}")
                            msg = "[CONNECT DEBUG] Active connection object"
                            print(f"{msg}: {active_conn}")
                        else:
                            # Direct ActiveConnection object
                            active_conn = connection_result
                            msg = "[CONNECT DEBUG] Connection result is ActiveConnection"
                            print(f"{msg}: {active_conn}")

                        if active_conn:
                            has_get_id = hasattr(active_conn, 'get_id')
                            conn_id = active_conn.get_id() if has_get_id else 'N/A'
                            print(f"[CONNECT DEBUG] Active connection ID: {conn_id}")
                            has_get_state = hasattr(active_conn, 'get_state')
                            conn_state = active_conn.get_state() if has_get_state else 'N/A'
                            msg = "[CONNECT DEBUG] Active connection state"
                            print(f"{msg}: {conn_state}")
                            # Check for connection error
                            try:
                                if hasattr(active_conn, 'get_state'):
                                    conn_state = active_conn.get_state()
                                    msg = "[CONNECT DEBUG] ActiveConnection state"
                                    print(f"{msg}: {conn_state}")
                                    # Check if there's error information
                                    if hasattr(active_conn, 'get_error'):
                                        error = active_conn.get_error()
                                        if error:
                                            msg = "[CONNECT DEBUG] ActiveConnection error"
                                            print(f"{msg}: {error}")
                            except Exception as e:
                                msg = f"[CONNECT DEBUG] Could not get ActiveConnection details: {e}"
                                print(msg)

                        # Check device state immediately
                        if self.wifi_device:
                            device_state = self.wifi_device.get_state()
                            msg = "[CONNECT DEBUG] Device state after connection"
                            print(f"{msg}: {device_state}")
                            if device_state == NM.DeviceState.FAILED:
                                print("[CONNECT DEBUG] WARNING: Device state is FAILED!")
                                # Check if there's a reason for the failure
                                try:
                                    active_conn_obj = self.wifi_device.get_active_connection()
                                    if active_conn_obj:
                                        msg = "[CONNECT DEBUG] Active connection exists"
                                        print(f"{msg}: {active_conn_obj}")
                                        if hasattr(active_conn_obj, 'get_state'):
                                            state = active_conn_obj.get_state()
                                            msg = "[CONNECT DEBUG] Active connection state"
                                            print(f"{msg}: {state}")
                                except Exception as e:
                                    msg = "[CONNECT DEBUG] Error checking active connection"
                                    print(f"{msg}: {e}")
                            active_ap = self.wifi_device.get_active_access_point()
                            print(f"[CONNECT DEBUG] Active access point: {active_ap}")

                        msg = f"Connecting to {ssid_str}..."
                        GLib.idle_add(self.status_label.set_text, msg)
                        # Wait for device to be fully connected before refreshing
                        # Check connection state with a delay to ensure it's established
                        max_checks = 30  # Maximum 15 seconds (30 * 500ms)
                        check_count = [0]  # Use list to allow modification in nested function

                        def check_and_refresh():
                            check_count[0] += 1
                            if check_count[0] > max_checks:
                                # Timeout - refresh anyway
                                msg = f"[CONNECT DEBUG] Connection timed out after {max_checks} checks"
                                print(msg)
                                if self.wifi_device:
                                    final_state = self.wifi_device.get_state()
                                    msg = "[CONNECT DEBUG] Final device state"
                                    print(f"{msg}: {final_state}")
                                    active_conn = self.wifi_device.get_active_connection()
                                    msg = "[CONNECT DEBUG] Final active connection"
                                    print(f"{msg}: {active_conn}")
                                msg = f"Connection to {ssid_str} timed out"
                                self.status_label.set_text(msg)
                                self._enable_refresh()
                                return False

                            if self.wifi_device:
                                state = self.wifi_device.get_state()
                                # Debug: print state for troubleshooting
                                check_num = check_count[0]
                                msg = f"[CONNECT DEBUG] Device state check #{check_num}"
                                print(f"{msg}: {state}")

                                # Also check if there's an active connection
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

                                # Check if device is connected or connecting
                                if state == NM.DeviceState.ACTIVATED:
                                    # Fully connected!
                                    self.status_label.set_text(f"Connected to {ssid_str}")
                                    self._refresh_after_connect()
                                    return False  # Don't repeat
                                connecting_states = (
                                    NM.DeviceState.IP_CONFIG,
                                    NM.DeviceState.IP_CHECK,
                                    NM.DeviceState.PREPARE,
                                    NM.DeviceState.CONFIG,
                                    NM.DeviceState.NEED_AUTH
                                )
                                if state in connecting_states:
                                    # Still connecting, check again in 0.5 seconds
                                    GLib.timeout_add(500, check_and_refresh)
                                    return False  # Don't repeat this call
                                if state == NM.DeviceState.DISCONNECTED:
                                    # Device is disconnected - but might be starting connection
                                    check_num = check_count[0]
                                    print(f"[CONNECT DEBUG] Device is DISCONNECTED (check #{check_num})")
                                    ac_exists = active_connection is not None
                                    print(f"[CONNECT DEBUG] Active connection exists: {ac_exists}")
                                    if active_connection:
                                        msg = "[CONNECT DEBUG] Active connection details"
                                        print(f"{msg}: {active_connection}")
                                    # Give it more time if we just started (first few checks)
                                    if check_count[0] <= 3:
                                        # Still early, connection might be starting
                                        print("[CONNECT DEBUG] Still early, waiting...")
                                        GLib.timeout_add(500, check_and_refresh)
                                        return False
                                    if active_connection:
                                        # We have an active connection but device is disconnected?
                                        # This is unusual, but give it a bit more time
                                        print("[CONNECT DEBUG] Active connection exists but device disconnected")
                                        if check_count[0] <= 6:
                                            GLib.timeout_add(500, check_and_refresh)
                                            return False
                                    # Connection failed
                                    check_num = check_count[0]
                                    msg = f"[CONNECT DEBUG] Connection failed after {check_num} checks"
                                    print(msg)
                                    if active_connection:
                                        msg = "[CONNECT DEBUG] Active connection object but device never connected"
                                        print(msg)
                                    msg = f"Connection to {ssid_str} failed (disconnected)"
                                    self.status_label.set_text(msg)
                                    self._enable_refresh()
                                    return False  # Don't repeat
                                if state == NM.DeviceState.FAILED:
                                    # Connection failed - check why
                                    print("[CONNECT DEBUG] Device state is FAILED")
                                    try:
                                        active_conn = self.wifi_device.get_active_connection()
                                        if active_conn and hasattr(active_conn, 'get_state'):
                                            conn_state = active_conn.get_state()
                                            msg = "[CONNECT DEBUG] ActiveConnection state "
                                            msg += "when device failed"
                                            print(f"{msg}: {conn_state}")
                                        # Check if password is needed
                                        has_state = hasattr(active_connection, 'get_state')
                                        if active_connection and has_state:
                                            ac_state = active_connection.get_state()
                                            msg = "[CONNECT DEBUG] ActiveConnection state"
                                            print(f"{msg}: {ac_state}")
                                    except Exception as e:
                                        print(f"[CONNECT DEBUG] Error getting failure details: {e}")

                                    msg = f"Connection to {ssid_str} failed."
                                    msg += " Password may be required."
                                    self.status_label.set_text(msg)
                                    self._enable_refresh()
                                    return False  # Don't repeat
                                if state in (NM.DeviceState.UNMANAGED, NM.DeviceState.UNAVAILABLE):
                                    # Connection failed or device unavailable
                                    state_names = {
                                        NM.DeviceState.UNMANAGED: "unmanaged",
                                        NM.DeviceState.UNAVAILABLE: "unavailable"
                                    }
                                    state_name = state_names.get(state, "unknown")
                                    msg = f"Connection to {ssid_str} failed ({state_name})"
                                    self.status_label.set_text(msg)
                                    self._enable_refresh()
                                    return False  # Don't repeat
                                # Unknown state - wait a bit more before giving up
                                if check_count[0] < 5:
                                    # Give it a few more tries for unknown states
                                    GLib.timeout_add(500, check_and_refresh)
                                    return False  # Don't repeat this call
                                # After several attempts, treat as failed
                                msg = f"Connection to {ssid_str} failed (state: {state})"
                                self.status_label.set_text(msg)
                                self._enable_refresh()
                                return False  # Don't repeat
                            # No device, just refresh after delay
                            self.status_label.set_text(f"Connected to {ssid_str}")
                            self._refresh_after_connect()
                            return False  # Don't repeat

                        # Start checking connection state after a longer delay (2 seconds)
                        # Connection might take a moment to start
                        GLib.timeout_add_seconds(2, check_and_refresh)
                    else:
                        # Connection result is None/False - connection failed to start
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
                        # Check if there's an error in the result
                        if hasattr(result, 'get_error'):
                            try:
                                error = result.get_error()
                                if error:
                                    print(f"[CONNECT DEBUG] Error from result: {error}")
                            except Exception:
                                pass
                        error_text = f"Failed to start connection to {ssid_str}."
                        error_text += " Password may be required."
                        GLib.idle_add(self.status_label.set_text, error_text)
                        GLib.idle_add(self._enable_refresh)
                except Exception as e:
                    error_msg = f"Connection error: {str(e)}"
                    print(f"[CONNECT DEBUG] Exception in connect callback: {error_msg}")
                    print("[CONNECT DEBUG] Full traceback:")
                    print(traceback.format_exc())
                    if self.wifi_device:
                        try:
                            device_state = self.wifi_device.get_state()
                            msg = "[CONNECT DEBUG] Device state during exception"
                            print(f"{msg}: {device_state}")
                        except Exception:
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
                except Exception as e:
                    error_msg = f"Failed to start connection: {str(e)}"
                    print(f"[CONNECT DEBUG] Exception starting async connection: {error_msg}")
                    print("[CONNECT DEBUG] Full traceback:")
                    print(traceback.format_exc())
                    self.status_label.set_text(error_msg)
                    self._enable_refresh()
                return False

            # Run connection in main thread
            GLib.idle_add(do_connect)

        except Exception as e:
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
                    except Exception as e:
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

        except Exception as e:
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
