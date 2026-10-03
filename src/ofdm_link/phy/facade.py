"""Supported application-facing PHY interface.

The package root retains legacy low-level imports for compatibility.  New
runtime callers should depend on this deliberately smaller facade.
"""

from .burst import (
    BurstConfig,
    BurstDecodeError,
    DecodedBurst,
    encode_burst,
)
from .codec import (
    CURRENT_PROTOCOL_VERSION,
    MCS,
    Frame,
    FrameKind,
    deserialize_frame,
    serialize_frame,
)
from .streaming import StreamingBurstDecoder

__all__ = [
    "BurstConfig",
    "BurstDecodeError",
    "CURRENT_PROTOCOL_VERSION",
    "DecodedBurst",
    "Frame",
    "FrameKind",
    "MCS",
    "StreamingBurstDecoder",
    "deserialize_frame",
    "encode_burst",
    "serialize_frame",
]
