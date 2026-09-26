import io

import pytest
from PIL import Image, ImageDraw

from weiner_beamer import designs, dialects, geometry as g
from weiner_beamer.store import MACHINE_DEFAULTS, deep_merge

DOG = deep_merge(MACHINE_DEFAULTS["dog"], {})


def machine(**over):
    return deep_merge(MACHINE_DEFAULTS, {"id": "m", **over})


def test_hatch_fills_square():
    segs = g.hatch([g.rect_poly(10, 10)], 1.0, 0)
    assert len(segs) == 10
    assert all(abs(abs(s[1][0] - s[0][0]) - 10) < 1e-6 for s in segs)
    # boustrophedon: consecutive rows run opposite directions
    assert segs[0][0][0] < segs[0][1][0] and segs[1][0][0] > segs[1][1][0]


def test_hatch_even_odd_hole():
    outer = g.rect_poly(10, 10)
    inner = g.rect_poly(4, 4)
    total = sum(abs(s[1][0] - s[0][0]) for s in g.hatch([outer, inner], 0.5, 0))
    assert total == pytest.approx((100 - 16) / 0.5, rel=0.02)


def test_text_fits_safe_zone():
    ops = designs.build("text", {"text": "Go Team!", "mode": "both"}, DOG)
    zw, zh = g.safe_zone(DOG)
    x0, y0, x1, y1 = g.bbox(ops)
    assert x1 - x0 <= zw + 0.01 and y1 - y0 <= zh + 0.01
    assert abs((x0 + x1) / 2) < 0.01 and abs((y0 + y1) / 2) < 0.01


def test_text_rejects_empty():
    with pytest.raises(designs.DesignError):
        designs.build("text", {"text": "  "}, DOG)


@pytest.mark.parametrize("pattern", designs.PATTERNS)
def test_patterns_stay_in_zone(pattern):
    ops = designs.build("pattern", {"pattern": pattern}, DOG)
    assert ops
    zw, zh = g.safe_zone(DOG)
    x0, y0, x1, y1 = g.bbox(ops)
    assert x0 >= -zw / 2 - 1e-3 and x1 <= zw / 2 + 1e-3
    assert y0 >= -zh / 2 - 1e-3 and y1 <= zh / 2 + 1e-3


def test_image_raster():
    im = Image.new("RGB", (200, 50), "white")
    ImageDraw.Draw(im).rectangle([50, 10, 150, 40], fill="black")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    ops = designs.build("image", {"line_interval": 0.2}, DOG, buf.getvalue())
    assert ops and all(len(o["pts"]) == 2 for o in ops)
    with pytest.raises(designs.DesignError):
        designs.build("image", {}, DOG, b"not an image")


def test_grbl_dialect_scales_power_and_resumes():
    m = machine(laser={"s_max": 1000, "power_scale": 0.5})
    ops = [g.op([(0, 0), (1, 0)], 1.0, 800), g.op([(0, 1), (1, 1)], 1.0, 800)]
    lines = dialects.GrblDialect().job(ops, m)
    text = "\n".join(ln.text for ln in lines)
    assert "M4 S0" in text and "S500" in text and text.strip().endswith("M5")
    burn_ops = {ln.op for ln in lines if ln.safe and ln.op >= 0}
    assert burn_ops == {0, 1}
    resumed = dialects.GrblDialect().job(ops, m, start_op=1)
    assert {ln.op for ln in resumed if ln.op >= 0} == {1}


def test_marlin_fan_mode_syncs_laser():
    m = machine(firmware="marlin", laser={"mode": "fan", "s_max": 255})
    lines = [ln.text for ln in dialects.MarlinDialect().job([g.op([(0, 0), (5, 0)], 1.0, 600)], m)]
    i = lines.index("M106 S255")
    assert lines[i - 1] == "M400" and "M107" in lines[i:]
    assert "M83" in lines  # no stray extruder moves


def test_slot_offset_and_rotation():
    m = machine(jig={"slots": 3, "pitch_y": 30}, align={"angle": 90})
    placed = dialects.place([g.op([(10, 0)], 1, 1)], m, designs.slot_offset(m, 2))
    x, y, z = placed[0]["pts"][0]
    assert x == pytest.approx(0, abs=1e-9) and y == pytest.approx(70) and z is None


def test_z_follow_drops_on_flanks():
    m = machine(dog={"z_follow": True, "diameter": 24})
    placed = dialects.place([g.op([(0, -6), (0, 6)], 1, 1)], m)
    zs = [p[2] for p in placed[0]["pts"]]
    assert max(zs) == pytest.approx(0, abs=1e-6)
    assert zs[0] < -1 and zs[-1] < -1


def test_template_has_every_slot():
    m = machine(jig={"slots": 4, "pitch_y": 30})
    ops = designs.build_template(m)
    ys = {round(o["pts"][0][1] / 30) for o in ops}
    assert {0, 1, 2, 3} <= ys
