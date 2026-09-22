# Sonos UPnP Bridge — present Sonos players as standard UPnP/DLNA MediaRenderers.
# Copyright (c) 2026 Lewis Menzies (Music Duck / MusicD)
# Released under the MIT License.  See the LICENSE file for details.
#
# SPDX-License-Identifier: MIT
"""A UPnP/DLNA MediaRenderer bridge that makes Sonos players visible to
control points - such as Audirvana - that do not speak Sonos natively."""

from .config import BRIDGE_NAME, BRIDGE_VERSION

__all__ = ["BRIDGE_NAME", "BRIDGE_VERSION", "__version__"]
__version__ = BRIDGE_VERSION
