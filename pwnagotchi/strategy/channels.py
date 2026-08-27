"""Channel selection strategy - core to agent behavior."""

import logging
import pwnagotchi.utils
from .statistics import ChannelStatistics


class ChannelStrategy:
    """
    Intelligent channel selection strategy.

    Strategy:
    - Always scan channels with active APs (high probability of captures)
    - Add random unscanned channels each epoch (explore new areas)
    - Track statistics per channel to guide future decisions
    """

    def __init__(self, config, logger=None):
        """
        Initialize channel strategy.

        Args:
            config: pwnagotchi configuration dict
            logger: optional logger instance
        """
        self.logger = logger or logging.getLogger(__name__)
        self.config = config
        self.stats = ChannelStatistics()

        # Configuration options
        self.extra_channels = config.get("main", {}).get("extra_channels", 15)
        self.restrict_channels = config.get("main", {}).get("restrict_channels", None)
        self.reset_history = config.get("main", {}).get("reset_history", True)
        # fraction of extra_channels kept as pure random exploration; the
        # rest is drawn weighted toward channels with a known success score
        explore_ratio = config.get("main", {}).get("channel_explore_ratio", 0.5)
        self.explore_ratio = max(0.0, min(1.0, explore_ratio))

    def select_channels(self, agent, access_points):
        """
        Select next set of channels to scan based on current APs and unscanned channels.

        Returns list of channels to scan in next epoch.
        """
        try:
            # Update active channels from current scan
            self.stats.update_active_channels(access_points)

            # Build next channel list: active + extra unscanned
            next_channels = self.stats.active_channels.copy()

            # Add unscanned channels for exploration: part pure random (keeps
            # discovering new territory), part weighted toward channels with
            # a known success score (epsilon-greedy bandit over chistos)
            n_extra = self.extra_channels
            if self.stats.unscanned_count() == 0:
                self._repopulate_unscanned_channels(agent)

            n_random = round(n_extra * self.explore_ratio)
            n_weighted = n_extra - n_random

            for _ in range(n_random):
                ch = self.stats.pop_random_unscanned()
                if ch is not None:
                    next_channels.append(ch)

            for _ in range(n_weighted):
                ch = self.stats.pop_weighted_unscanned()
                if ch is not None:
                    next_channels.append(ch)

            # Update agent config
            if hasattr(agent, "_config"):
                agent._config["personality"]["channels"] = next_channels

            self.logger.info(
                f"Active: {self.stats.active_channels}, "
                f"Next: {next_channels}, "
                f"Unscanned: {len(self.stats.unscanned_channels)}"
            )

            return next_channels

        except Exception as e:
            self.logger.error(f"Error selecting channels: {e}")
            return self.stats.active_channels

    def _repopulate_unscanned_channels(self, agent):
        """Repopulate unscanned channel list from config or agent."""
        try:
            # Try restrict_channels first
            if self.restrict_channels:
                self.logger.info("Repopulating from restrict_channels")
                self.stats.set_unscanned_channels(self.restrict_channels)
            # Try agent's allowed channels
            elif hasattr(agent, "_allowed_channels"):
                self.logger.info(f"Repopulating from allowed: {agent._allowed_channels}")
                self.stats.set_unscanned_channels(agent._allowed_channels)
            # Try agent's supported channels
            elif hasattr(agent, "_supported_channels"):
                self.logger.info("Repopulating from supported")
                self.stats.set_unscanned_channels(agent._supported_channels)
            # Fall back to all channels for interface
            else:
                self.logger.info("Repopulating from interface channels")
                iface = self.config.get("main", {}).get("iface", "wlan0")
                self.stats.set_unscanned_channels(pwnagotchi.utils.iface_channels(iface))

        except Exception as e:
            self.logger.warning(f"Error repopulating unscanned channels: {e}")

    def on_wifi_update(self, agent, access_points):
        """Called when agent updates its AP list."""
        self.stats.update_active_channels(access_points)

    def on_association(self, agent, access_point):
        """Called when sending association frame."""
        self.stats.record_interaction("Associations", access_point.get("channel", -1))
        self.stats.mark_ap_seen(access_point, "assoc")

    def on_deauthentication(self, agent, access_point, client_station):
        """Called when sending deauth."""
        self.stats.record_interaction("Deauths", access_point.get("channel", -1))
        self.stats.mark_ap_seen(access_point, "deauth")

    def on_handshake(self, agent, filename, access_point, client_station):
        """Called when handshake is captured."""
        self.stats.record_interaction("Handshakes", access_point.get("channel", -1))
        self.stats.mark_ap_seen(access_point, "handshake")

    def on_bcap_wifi_ap_new(self, agent, event):
        """Called when bettercap detects new AP."""
        try:
            ap = event.get("data", {})
            self.stats.mark_ap_seen(ap)
        except Exception as e:
            self.logger.debug(f"Error on_bcap_wifi_ap_new: {e}")

    def on_bcap_wifi_ap_lost(self, agent, event):
        """Called when bettercap loses AP."""
        try:
            ap = event.get("data", {})
            self.stats.record_ap_lost(ap)
        except Exception as e:
            self.logger.debug(f"Error on_bcap_wifi_ap_lost: {e}")

    def get_stats(self):
        """Get current strategy statistics."""
        return self.stats.get_stats()

    def load_stats(self, path):
        """Load persisted channel statistics from disk, if present."""
        try:
            status = pwnagotchi.utils.StatusFile(path, data_format="json")
            if status.data:
                retention_days = self.config.get("main", {}).get("stats_retention_days", 30)
                self.stats.load_dict(status.data, retention_days=retention_days)
                self.logger.info(f"Loaded channel statistics from {path}")
        except Exception as e:
            self.logger.warning(f"Error loading channel statistics: {e}")

    def save_stats(self, path):
        """Persist current channel statistics to disk."""
        try:
            pwnagotchi.utils.StatusFile(path, data_format="json").update(self.stats.to_dict())
        except Exception as e:
            self.logger.warning(f"Error saving channel statistics: {e}")
