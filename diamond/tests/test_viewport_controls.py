from diamond.analyzers.base import AnalysisContext
from diamond.analyzers.layout import LayoutAnalyzer


def test_overlay_controls_are_compared_to_actual_viewport_height():
    controls = [{"selector": "#map", "viewportBottom": 850}, {"selector": "#visible", "viewportBottom": 640},
                {"selector": "#scroll-content"}]
    ctx = AnalysisContext(None, None, [{"url": "https://example.test/", "type": "inventory",
                                        "viewport": {"w": 1280, "h": 667}, "elements": controls}])
    findings = LayoutAnalyzer().run(ctx).findings
    assert [(f.check_id, f.evidence["key"]) for f in findings] == [("layout.control_below_viewport", "#map")]
