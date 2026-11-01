import textwrap

import pytest
from la_fakes import context

from ofdm_message_link.la import loader
from ofdm_message_link.la.api import FeedbackReporter, LinkAdaptation, ReporterContext
from ofdm_message_link.la.examples import ArfLinkAdaptation
from ofdm_message_link.la.fixed import FixedMcs
from ofdm_message_link.la.olla import OllaLinkAdaptation
from ofdm_message_link.la.reporter import DefaultReporter

REPORTER_CONTEXT = ReporterContext(node_id=1, peer_node_id=2)


@pytest.mark.parametrize(
    ("spec", "cls"),
    [("fixed", FixedMcs), ("olla", OllaLinkAdaptation), ("arf", ArfLinkAdaptation)],
)
def test_builtin_algorithms(spec, cls):
    assert loader.load(spec, "la") is cls
    assert isinstance(loader.build_link_adaptation(spec, context()), LinkAdaptation)


def test_builtin_reporter():
    reporter = loader.build_reporter("default", REPORTER_CONTEXT, ["period_ms=50"])
    assert isinstance(reporter, DefaultReporter) and isinstance(reporter, FeedbackReporter)
    assert reporter.period_s == pytest.approx(0.05)


def test_module_spec_with_kwargs():
    la = loader.build_link_adaptation(
        "ofdm_message_link.la.examples:ArfLinkAdaptation", context(), ["up_after=5"]
    )
    assert isinstance(la, ArfLinkAdaptation) and la.up_after == 5


def test_file_spec(tmp_path):
    path = tmp_path / "my_la.py"
    path.write_text(textwrap.dedent("""
        from dataclasses import dataclass
        from ofdm_message_link.la.api import Decision

        @dataclass
        class Settings:
            label: str

        class Always:
            def __init__(self, context, mcs=9, label="x"):
                self.mcs = mcs
                self.settings = Settings(label)
            def on_sent(self, burst): pass
            def on_feedback(self, report): pass
            def decide(self, now_s): return Decision(self.mcs, self.settings.label)
            def snapshot(self): return {"mcs_index": self.mcs}
    """))
    la = loader.build_link_adaptation(f"{path}:Always", context(), ["mcs=12", "label=hello"])
    decision = la.decide(0.0)
    assert (decision.mcs_index, decision.reason) == (12, "hello")
    assert loader.load(f"{path}:Always", "la") is type(la)  # imported once


def test_parse_kwargs_literals_and_strings():
    assert loader.parse_kwargs(
        ["a=5", "b=0.02", "c=True", "d=hello", "e='quoted'", "f=[1, 2]", "g=", "h=a=b"]
    ) == {"a": 5, "b": 0.02, "c": True, "d": "hello", "e": "quoted", "f": [1, 2],
          "g": "", "h": "a=b"}
    assert loader.parse_kwargs(None) == {}


@pytest.mark.parametrize("pairs", [["a=1", "a=2"], ["novalue"], ["9x=1"]])
def test_parse_kwargs_errors(pairs):
    with pytest.raises(SystemExit) as info:
        loader.parse_kwargs(pairs)
    assert "\n" not in str(info.value)


def test_non_conforming_class_is_refused(tmp_path):
    path = tmp_path / "bad.py"
    path.write_text("class Half:\n    def __init__(self, context): pass\n"
                    "    def decide(self, now_s): return None\n")
    with pytest.raises(SystemExit, match="does not implement LinkAdaptation"):
        loader.build_link_adaptation(f"{path}:Half", context())


def test_reporter_without_reporter_id_is_refused(tmp_path):
    path = tmp_path / "rep.py"
    path.write_text(textwrap.dedent("""
        class NoId:
            def __init__(self, context): pass
            def on_preambles(self, now_s, snr_db, *, control_only): pass
            def on_burst(self, report): pass
            def on_rx_health(self, now_s, health): pass
            def due(self, now_s): return False
            def next_due_s(self, now_s): return None
            def build(self, now_s): raise NotImplementedError
    """))
    with pytest.raises(SystemExit, match="FeedbackReporter"):
        loader.build_reporter(f"{path}:NoId", REPORTER_CONTEXT)


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("nosuch", "not a built-in"),
        ("no_such_module_xyz:Class", "cannot import module"),
        ("ofdm_message_link.la.examples:Missing", "has no attribute"),
        ("ofdm_message_link.la.examples:__doc__", "is not a class"),
        ("/no/such/file.py:Class", "no such file"),
        ("olla:", "not a built-in"),
    ],
)
def test_bad_specs_end_in_one_line_system_exit(spec, message):
    with pytest.raises(SystemExit, match=message) as info:
        loader.build_link_adaptation(spec, context())
    assert str(info.value).startswith("--link-adaptation: ")
    assert "\n" not in str(info.value)


def test_file_that_fails_to_import(tmp_path):
    path = tmp_path / "broken.py"
    path.write_text("raise RuntimeError('boom')\n")
    with pytest.raises(SystemExit, match="importing .* failed: RuntimeError: boom"):
        loader.load(f"{path}:X", "la")


def test_bad_kwarg_and_bad_value_end_in_system_exit():
    with pytest.raises(SystemExit, match="OllaLinkAdaptation.*TypeError"):
        loader.build_link_adaptation("olla", context(), ["no_such_knob=1"])
    with pytest.raises(SystemExit, match="ValueError"):
        loader.build_link_adaptation("olla", context(), ["target_loss=2"])
    with pytest.raises(SystemExit, match="--feedback-reporter: .*ValueError"):
        loader.build_reporter("default", REPORTER_CONTEXT, ["period_ms=0"])


def test_fixed_accepts_any_transmit_visible_v5_index():
    la = loader.build_link_adaptation("fixed", context(), ["mcs_index=14"])
    assert la.decide(0.0).mcs_index == 14
    la.set_mcs(10)
    assert (la.decide(1.0).mcs_index, la.decide(1.0).reason) == (10, "fixed")
    with pytest.raises(SystemExit):
        loader.build_link_adaptation("fixed", context(), ["mcs_index=11"])
    with pytest.raises(ValueError):
        la.set_mcs(4)
