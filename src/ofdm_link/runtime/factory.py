"""Translate validated user configuration into runtime protocol objects."""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from ofdm_link.config import LinkConfig
from ofdm_link.phy import (
    CURRENT_BURST_HEADER_VERSION,
    TURBO_BURST_HEADER_VERSION,
    BurstConfig,
    DualVersionFrameDecoder,
    FrameCodec,
    FrameDecoder,
    OfdmNumerology,
    SoftSymbolDemapper,
    SymbolDemapper,
    SymbolMapper,
    SyncConfig,
    decode_fec_bits_native,
    decode_soft_fec_bits_native,
    demap_symbols,
    make_convolutional_frame_codec,
    make_dual_version_frame_decoder,
    make_soft_convolutional_frame_codec,
    make_turbo_frame_codec,
    make_uncoded_frame_codec,
    map_symbols,
    soft_demap_symbols,
)
from ofdm_link.phy.burst_header import (
    CONVOLUTIONAL_R13_BURST_HEADER_VERSION,
    UNCODED_BURST_HEADER_VERSION,
)
from ofdm_link.phy.mcs_table import McsTableEntry
from ofdm_link.phy.turbo import native_convolutional_soft_decoder


@dataclass(frozen=True, slots=True)
class _ObservationCompatibleFrameDecoder:
    """Preserve direct coded-bit calls while burst RX supplies session observations."""

    decoder: DualVersionFrameDecoder
    soft_session: bool

    def __call__(
        self,
        observations,
        *,
        wire_version: int = 1,
        information_bit_count: int | None = None,
    ):
        return self.decode_result(
            observations,
            wire_version=wire_version,
            information_bit_count=information_bit_count,
        ).frame

    def decode_result(
        self,
        observations,
        *,
        wire_version: int = 1,
        information_bit_count: int | None = None,
    ):
        values = np.asarray(observations)
        expects_soft = self.soft_session or wire_version == TURBO_BURST_HEADER_VERSION
        if (
            expects_soft
            and (np.issubdtype(values.dtype, np.integer) or np.issubdtype(values.dtype, np.bool_))
            and np.all((values == 0) | (values == 1))
        ):
            values = np.where(values == 0, 8.0, -8.0).astype(np.float32)
        return self.decoder.decode_result(
            values,
            wire_version=wire_version,
            information_bit_count=information_bit_count,
        )


@dataclass(frozen=True, slots=True)
class DecoderBackendSelection:
    """Selected paired frame codec and constellation callables."""

    frame_codec: FrameCodec
    decoder_backend: str
    symbol_mapper: SymbolMapper
    symbol_demapper: SymbolDemapper
    soft_symbol_demapper: SoftSymbolDemapper | None = None
    dual_frame_decoder: DualVersionFrameDecoder | _ObservationCompatibleFrameDecoder | None = None
    wire_version: int = CURRENT_BURST_HEADER_VERSION

    @property
    def frame_encoder(self):
        """Return the TX half paired with :attr:`frame_decoder`."""

        return self.frame_codec.encode

    @property
    def frame_decoder(self) -> FrameDecoder:
        """Return a receiver accepting both v1 convolutional and v2 Turbo."""

        return self.dual_frame_decoder or self.frame_codec.decode


def _convolutional_r13_codec(decision_mode: str) -> FrameCodec:
    """Build the wire-v3 rate-1/3 codec on the private native Viterbi.

    GNU Radio's ``cc_decoder`` only builds rate-1/2 graphs, so this rate uses
    the compiled Viterbi in the project's own extension for both decision
    modes; the hard path simply presents saturated LLRs.
    """

    decoder = native_convolutional_soft_decoder(3)
    if decision_mode == "soft":
        return make_soft_convolutional_frame_codec(
            rate_inverse=3,
            soft_decoder=decoder,
        )

    def hard_decoder(coded_bits):
        bits = np.asarray(coded_bits)
        return decoder(np.where(bits == 0, 8.0, -8.0).astype(np.float32))

    return make_convolutional_frame_codec(rate_inverse=3, hard_decoder=hard_decoder)


def _receiver_for(
    *,
    v2_codec: FrameCodec,
    decision_mode: str,
    v1_codec: FrameCodec | None = None,
) -> _ObservationCompatibleFrameDecoder:
    """Accept every defined wire version at one consistent decision mode.

    The burst layer demaps to LLRs for the whole session once a soft demapper
    is selected, so every version in the receiver must take the same kind of
    observation; mixing a hard v1 codec into a soft receiver would reject
    perfectly good frames.
    """

    soft = decision_mode in {"soft", "turbo"}
    selected_v1 = v1_codec or (
        make_soft_convolutional_frame_codec(soft_decoder=decode_soft_fec_bits_native)
        if soft
        else make_convolutional_frame_codec(hard_decoder=decode_fec_bits_native)
    )
    return _ObservationCompatibleFrameDecoder(
        make_dual_version_frame_decoder(
            v1_codec=selected_v1,
            v2_codec=v2_codec,
            v3_codec=_convolutional_r13_codec("soft" if soft else "hard"),
            v4_codec=make_uncoded_frame_codec(soft=soft),
        ),
        soft_session=soft,
    )


def select_frame_decoder(config: LinkConfig) -> DecoderBackendSelection:
    """Select the paired frame codec and constellation callables for ``config``.

    GNU Radio is imported lazily, only when a rate-1/2 convolutional frame is
    actually decoded.
    """

    _require_link_config(config)
    decision_mode = config.phy.fec.decision_mode
    soft_observations = decision_mode in {"soft", "turbo"}
    v1_codec = (
        make_soft_convolutional_frame_codec(soft_decoder=decode_soft_fec_bits_native)
        if soft_observations
        else make_convolutional_frame_codec(hard_decoder=decode_fec_bits_native)
    )
    return _select_frame_decoder(
        config,
        v1_codec=v1_codec,
        turbo_codec=make_turbo_frame_codec(
            max_iterations=config.phy.fec.max_iterations
        ),
    )


def select_frame_decoder_for_entry(
    config: LinkConfig,
    entry: McsTableEntry,
) -> DecoderBackendSelection:
    """Select the paired codecs one MCS-table entry names.

    The entry overrides only the FEC half of the configuration; every other
    validated setting, including the acceleration backend, is preserved.
    """

    _require_link_config(config)
    decision_mode = config.phy.fec.decision_mode
    if entry.fec_scheme == "turbo":
        decision_mode = "turbo"
    elif decision_mode == "turbo":
        decision_mode = "soft"
    overridden = replace(
        config,
        phy=replace(
            config.phy,
            fec=replace(
                config.phy.fec,
                scheme=entry.fec_scheme,
                code_rate=entry.code_rate,
                decision_mode=decision_mode,
            ),
        ),
    )
    return select_frame_decoder(overridden)


def _select_frame_decoder(
    config: LinkConfig,
    *,
    v1_codec: FrameCodec,
    turbo_codec: FrameCodec,
) -> DecoderBackendSelection:
    """Apply the one decoder/backend policy to caller-owned codec resources."""

    decision_mode = config.phy.fec.decision_mode
    soft_observations = decision_mode in {"soft", "turbo"}
    receiver = _receiver_for(
        v1_codec=v1_codec,
        v2_codec=turbo_codec,
        decision_mode=decision_mode,
    )
    if config.phy.fec.scheme == "uncoded":
        return DecoderBackendSelection(
            make_uncoded_frame_codec(soft=soft_observations),
            f"uncoded_r1_{decision_mode}",
            map_symbols,
            demap_symbols,
            soft_demap_symbols if soft_observations else None,
            receiver,
            UNCODED_BURST_HEADER_VERSION,
        )
    if config.phy.fec.scheme == "convolutional" and config.phy.fec.code_rate == "1/3":
        return DecoderBackendSelection(
            _convolutional_r13_codec(decision_mode),
            f"native_viterbi_r1/3_{decision_mode}",
            map_symbols,
            demap_symbols,
            soft_demap_symbols if soft_observations else None,
            receiver,
            CONVOLUTIONAL_R13_BURST_HEADER_VERSION,
        )
    if config.phy.fec.scheme == "turbo":
        return DecoderBackendSelection(
            turbo_codec,
            "native_turbo_max_log_map",
            map_symbols,
            demap_symbols,
            soft_demap_symbols,
            receiver,
            TURBO_BURST_HEADER_VERSION,
        )
    if decision_mode == "soft":
        return DecoderBackendSelection(
            v1_codec,
            "gnuradio_native_soft",
            map_symbols,
            demap_symbols,
            soft_demap_symbols,
            receiver,
        )
    if config.acceleration.backend == "native":
        return DecoderBackendSelection(
            v1_codec,
            "gnuradio_native",
            map_symbols,
            demap_symbols,
            dual_frame_decoder=receiver,
        )
    raise ValueError(f"unsupported acceleration backend {config.acceleration.backend!r}")


def _require_link_config(config: LinkConfig) -> None:
    if not isinstance(config, LinkConfig):
        raise TypeError("config must be a LinkConfig")


def build_burst_config(config: LinkConfig) -> BurstConfig:
    """Build the burst configuration this link configuration asks for.

    Public because every caller that decodes a burst -- the inline receiver and
    each decode-pool worker -- must acquire with the same configured
    thresholds.  A caller that builds its own :class:`BurstConfig` silently
    gets the library defaults instead.
    """

    _require_link_config(config)
    if config.phy.fft_len != 64 or config.phy.cyclic_prefix_len != 16:
        raise ValueError(
            "the burst format supports only the 64-point FFT, 16-sample CP, "
            "and fixed 48-data/4-pilot carrier allocation"
        )
    numerology = OfdmNumerology(
        fft_size=config.phy.fft_len,
        cp_length=config.phy.cyclic_prefix_len,
    )
    sync = SyncConfig(
        fft_size=config.phy.fft_len,
        cyclic_prefix_length=config.phy.cyclic_prefix_len,
        detection_threshold=config.phy.sync.detection_threshold,
        correlation_threshold=config.phy.sync.correlation_threshold,
    )
    return BurstConfig(numerology=numerology, sync=sync)
