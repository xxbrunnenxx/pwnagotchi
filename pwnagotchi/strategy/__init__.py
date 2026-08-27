"""Pwnagotchi channel selection strategy - core to agent behavior.

This module replaces the removed RL-based AI with intelligent heuristics:
- Track active channels (have APs) and prioritize them
- Explore unscanned channels randomly
- Maintain statistics per channel
- Guide agent behavior through channel selection

Originally extracted from auto-tune plugin to make it core functionality.
"""

import logging
from .channels import ChannelStrategy
from .statistics import ChannelStatistics


class Strategy:
    """
    Main strategy facade.

    Integrates channel selection with agent lifecycle and event handling.
    """

    # how many epochs between periodic stats saves (also saved on reboot/restart)
    SAVE_EVERY_N_EPOCHS = 10

    def __init__(self, config, logger=None):
        self.logger = logger or logging.getLogger(__name__)
        self.channels = ChannelStrategy(config, logger=self.logger)
        self.config = config
        self._stats_file = config.get("main", {}).get(
            "channel_stats_file", "/etc/pwnagotchi/channel_stats.json"
        )
        self._epochs_since_save = 0
        self.channels.load_stats(self._stats_file)

    def start(self):
        """Initialize strategy when agent starts."""
        self.logger.info("Channel selection strategy initialized")

    def save_stats(self):
        """Persist channel statistics to disk immediately."""
        self.channels.save_stats(self._stats_file)

    def on_epoch(self):
        """Called at each epoch boundary; saves stats every SAVE_EVERY_N_EPOCHS."""
        self._epochs_since_save += 1
        if self._epochs_since_save >= self.SAVE_EVERY_N_EPOCHS:
            self._epochs_since_save = 0
            self.save_stats()

    def select_next_channels(self, agent, access_points):
        """
        Select channels for next epoch.

        Called after WiFi scan completes. Returns list of channels to scan.
        """
        return self.channels.select_channels(agent, access_points)

    # Event handlers - connect to agent event system
    def on_wifi_update(self, agent, access_points):
        """WiFi list updated."""
        self.channels.on_wifi_update(agent, access_points)

    def on_association(self, agent, access_point):
        """Association sent."""
        self.channels.on_association(agent, access_point)

    def on_deauthentication(self, agent, access_point, client_station):
        """Deauthentication sent."""
        self.channels.on_deauthentication(agent, access_point, client_station)

    def on_handshake(self, agent, filename, access_point, client_station):
        """Handshake captured."""
        self.channels.on_handshake(agent, filename, access_point, client_station)

    def on_bcap_wifi_ap_new(self, agent, event):
        """Bettercap: new AP detected."""
        self.channels.on_bcap_wifi_ap_new(agent, event)

    def on_bcap_wifi_ap_lost(self, agent, event):
        """Bettercap: AP lost."""
        self.channels.on_bcap_wifi_ap_lost(agent, event)

    def get_stats(self):
        """Get current strategy statistics."""
        return self.channels.get_stats()


__all__ = ["Strategy", "ChannelStrategy", "ChannelStatistics"]
