"""
PDF Annotator — Streamlit Version (Fixed & Feature-Complete)
ปรับปรุงจากต้นฉบับ Tkinter:
  - แก้บั๊กพิกัด commit_canvas_to_pdf (line/arrow/circle)
  - เพิ่มระบบ Undo + Redo ที่สมบูรณ์
  - เพิ่มการจัดการหน้า (ลบ, แทรกหน้าว่าง, แทรก PDF, หมุน)
  - เพิ่ม Text Overlay แบบระบุตำแหน่ง X/Y จริงบน PDF
  - แก้ Tool state ให้ sync กับ canvas mode (arrow ≠ line)
  - ปรับ highlight detection ให้ robust
  - Sidebar Thumbnail navigation
"""

import streamlit as st
import fitz  # PyMuPDF
from PIL import Image
import copy, io, os

try:
    from streamlit_drawable_canvas import st_canvas
    HAS_CANVAS = True
except ImportError:
    HAS_CANVAS = False

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────
TOOL_SELECT    = "select"
TOOL_TEXT      = "text"
TOOL_RECT      = "rect"
TOOL_CIRCLE    = "ellipse"
TOOL_LINE      = "line"
TOOL_ARROW     = "arrow"
TOOL_HIGHLIGHT = "highlight"
TOOL_REDACT    = "redact"
TOOL_PEN       = "pen"

ZOOM_STEP = 0.25
ZOOM_MIN  = 0.5
ZOOM_MAX  = 4.0
MAX_UNDO  = 15

HL_FILL   = "rgba(255,255,0,0.35)"
HL_STROKE = "rgba(255,220,0,0.8)"

EYEDROP_TARGETS = ["pen_color", "annot_color", "text_color", "redact_color"]

# ─────────────────────────────────────────────
# Colour helpers
# ─────────────────────────────────────────────
def hex_to_rgb01(h: str):
    h = h.lstrip("#")
    return int(h[0:2],16)/255, int(h[2:4],16)/255, int(h[4:6],16)/255

def rgb_to_hex(r: int, g: int, b: int) -> str:
    return "#{:02x}{:02x}{:02x}".format(r, g, b)

def sample_color_from_page(img: Image.Image, cx: int, cy: int,
                            radius: int = 3) -> str:
    """ดูดสีเฉลี่ยจาก pixel บริเวณ (cx, cy) ± radius บนรูปหน้า PDF"""
    arr = img.convert("RGB")
    w, h = arr.size
    x0 = max(0, cx - radius); x1 = min(w, cx + radius + 1)
    y0 = max(0, cy - radius); y1 = min(h, cy + radius + 1)
    pixels = [arr.getpixel((x, y)) for x in range(x0, x1) for y in range(y0, y1)]
    if not pixels:
        return "#000000"
    r = int(sum(p[0] for p in pixels) / len(pixels))
    g = int(sum(p[1] for p in pixels) / len(pixels))
    b = int(sum(p[2] for p in pixels) / len(pixels))
    return rgb_to_hex(r, g, b)

def rgba_to_rgb01(s: str):
    """'rgba(r,g,b,a)' or '#rrggbb' → (r,g,b) in 0-1"""
    s = s.strip()
    if s.startswith("#"):
        return hex_to_rgb01(s)
    try:
        vals = s.replace("rgba(","").replace("rgb(","").replace(")","").split(",")
        return int(vals[0])/255, int(vals[1])/255, int(vals[2])/255
    except Exception:
        return 0.0, 0.0, 0.0

def is_highlight_obj(obj: dict) -> bool:
    """ตรวจว่า object บน Canvas เป็น highlight (สีเหลืองโปร่งใส)"""
    fill = obj.get("fill", "")
    stroke = obj.get("stroke", "")
    for s in (fill, stroke):
        if "255,255,0" in s.replace(" ","") or "255, 255, 0" in s:
            return True
    return False

def is_redact_obj(obj: dict) -> bool:
    fill = obj.get("fill", "")
    if not fill:
        return False
    s = fill.replace(" ","")
    # ถ้า fill มีค่าและ opacity > 0.5 ถือว่า redact
    if s.startswith("rgba"):
        try:
            parts = s.replace("rgba(","").replace(")","").split(",")
            alpha = float(parts[3])
            return alpha > 0.5
        except Exception:
            return False
    if s.startswith("#") and s != "rgba(0,0,0,0)":
        return True
    return False

# ─────────────────────────────────────────────
# Session state init
# ─────────────────────────────────────────────
def init_state():
    defaults = {
        "doc":          None,
        "doc_name":     "",
        "current_page": 0,
        "zoom":         1.0,
        "tool":         TOOL_PEN,
        "pen_color":    "#c0392b",
        "annot_color":  "#f1c40f",
        "text_color":   "#1a5276",
        "redact_color": "#000000",
        "line_width":   2,
        "font_size":    14,
        "undo_stack":      [],
        "redo_stack":      [],
        "canvas_key":      0,
        "eyedrop_mode":    False,
        "eyedrop_target":  "pen_color",
        "eyedrop_result":  "",
        # Font management
        "font_bytes":      None,
        "font_name":       "Helvetica",
        "font_source":     "builtin",
        # Draft layer — objects ที่วาดแต่ยังไม่ commit ลง PDF (drag-able)
        # dict keyed by page_number → list of Fabric.js object dicts
        "draft_objects":   {},
        # สำหรับ text overlay draft (แยกจาก canvas)
        "text_drafts":     {},  # page → list of {text,x,y,font_size,color,font_name,font_source,font_bytes,font_path}
        "selected_text_idx": None,  # index ใน text_drafts[page] ที่เลือกอยู่
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

# ─────────────────────────────────────────────
# Undo / Redo
# ─────────────────────────────────────────────
def _snapshot() -> dict | None:
    doc = st.session_state.doc
    if not doc:
        return None
    return {
        "pdf_bytes":     doc.tobytes(),
        "current_page":  st.session_state.current_page,
        "draft_objects": copy.deepcopy(st.session_state.draft_objects),
        "text_drafts":   copy.deepcopy(st.session_state.text_drafts),
    }

def save_state():
    snap = _snapshot()
    if snap:
        st.session_state.undo_stack.append(snap)
        if len(st.session_state.undo_stack) > MAX_UNDO:
            st.session_state.undo_stack.pop(0)
        st.session_state.redo_stack.clear()

def do_undo():
    if not st.session_state.undo_stack:
        return
    cur = _snapshot()
    if cur:
        st.session_state.redo_stack.append(cur)
    snap = st.session_state.undo_stack.pop()
    _restore(snap)

def do_redo():
    if not st.session_state.redo_stack:
        return
    cur = _snapshot()
    if cur:
        st.session_state.undo_stack.append(cur)
    snap = st.session_state.redo_stack.pop()
    _restore(snap)

def _restore(snap: dict):
    if st.session_state.doc:
        st.session_state.doc.close()
    st.session_state.doc           = fitz.open("pdf", snap["pdf_bytes"])
    st.session_state.current_page  = snap["current_page"]
    st.session_state.draft_objects = copy.deepcopy(snap.get("draft_objects", {}))
    st.session_state.text_drafts   = copy.deepcopy(snap.get("text_drafts", {}))
    st.session_state.selected_text_idx = None
    st.session_state.canvas_key   += 1

# ─────────────────────────────────────────────
# PDF helpers
# ─────────────────────────────────────────────
def get_page_image() -> Image.Image | None:
    doc = st.session_state.doc
    if not doc:
        return None
    page = doc[st.session_state.current_page]
    mat  = fitz.Matrix(st.session_state.zoom, st.session_state.zoom)
    pix  = page.get_pixmap(matrix=mat, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

def total_pages() -> int:
    return len(st.session_state.doc) if st.session_state.doc else 0

def go_page(n: int):
    if st.session_state.doc and 0 <= n < total_pages():
        st.session_state.current_page = n
        st.session_state.canvas_key  += 1

# ─────────────────────────────────────────────
# Page management
# ─────────────────────────────────────────────
def delete_current_page():
    doc = st.session_state.doc
    if not doc or len(doc) <= 1:
        st.warning("ไม่สามารถลบได้ — ต้องมีอย่างน้อย 1 หน้า")
        return
    save_state()
    p = st.session_state.current_page
    doc.delete_page(p)
    if p >= len(doc):
        st.session_state.current_page = len(doc) - 1
    st.session_state.canvas_key += 1

def insert_blank_page():
    doc = st.session_state.doc
    if not doc:
        return
    save_state()
    after = st.session_state.current_page
    doc.new_page(width=595, height=842, after=after)
    st.session_state.canvas_key += 1

def rotate_current_page(angle: int = 90):
    doc = st.session_state.doc
    if not doc:
        return
    save_state()
    page = doc[st.session_state.current_page]
    cur  = page.rotation if hasattr(page, "rotation") else 0
    page.set_rotation((cur + angle) % 360)
    st.session_state.canvas_key += 1

def delete_all_annots():
    doc = st.session_state.doc
    if not doc:
        return
    page   = doc[st.session_state.current_page]
    annots = list(page.annots())
    if annots:
        save_state()
        for a in annots:
            page.delete_annot(a)
        st.session_state.canvas_key += 1

# ─────────────────────────────────────────────
# Font helpers
# ─────────────────────────────────────────────

# Built-in PyMuPDF base-14 fonts
BUILTIN_FONTS = {
    "Helvetica":             "helv",
    "Helvetica Bold":        "hebo",
    "Helvetica Oblique":     "heio",
    "Helvetica Bold Oblique":"hebo",
    "Times Roman":           "tiro",
    "Times Bold":            "tibo",
    "Times Italic":          "tiit",
    "Times Bold Italic":     "tibi",
    "Courier":               "cour",
    "Courier Bold":          "cobo",
    "Courier Oblique":       "coit",
    "Courier Bold Oblique":  "cobi",
    "Symbol":                "symb",
    "ZapfDingbats":          "zadb",
}

def scan_system_fonts() -> dict[str, str]:
    """สแกนหา .ttf/.otf จาก directory มาตรฐานบนทุก OS → {ชื่อ: path}"""
    import platform, glob
    dirs = []
    sys = platform.system()
    if sys == "Windows":
        dirs = [r"C:\Windows\Fonts"]
    elif sys == "Darwin":
        dirs = ["/Library/Fonts", "/System/Library/Fonts",
                os.path.expanduser("~/Library/Fonts")]
    else:  # Linux / Streamlit Cloud
        dirs = ["/usr/share/fonts", "/usr/local/share/fonts",
                os.path.expanduser("~/.fonts"),
                os.path.expanduser("~/.local/share/fonts")]

    fonts: dict[str, str] = {}
    for d in dirs:
        for ext in ("*.ttf", "*.otf", "*.TTF", "*.OTF"):
            for path in glob.glob(os.path.join(d, "**", ext), recursive=True):
                name = os.path.splitext(os.path.basename(path))[0]
                fonts[name] = path
    return fonts

def render_font_preview(font_bytes: bytes | None, font_path: str | None,
                        font_name: str, size: int = 22) -> str:
    """สร้าง base64 PNG ตัวอย่างข้อความจาก font → data URI"""
    try:
        from PIL import ImageFont, ImageDraw
        import base64, io as _io
        preview_text = f"AaBbCc 1234 ภาษาไทย"
        img = Image.new("RGB", (480, 56), "#ffffff")
        draw = ImageDraw.Draw(img)
        try:
            if font_bytes:
                fnt = ImageFont.truetype(_io.BytesIO(font_bytes), size)
            elif font_path and os.path.exists(font_path):
                fnt = ImageFont.truetype(font_path, size)
            else:
                fnt = ImageFont.load_default()
        except Exception:
            fnt = ImageFont.load_default()
        draw.text((8, 8), preview_text, font=fnt, fill="#1a1a2e")
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/png;base64,{b64}"
    except Exception:
        return ""

# ─────────────────────────────────────────────
# Text insertion
# ─────────────────────────────────────────────
def add_text_to_draft(text: str, x_pct: float, y_pct: float):
    """เพิ่มข้อความเข้า draft layer (ยังไม่ commit ลง PDF — drag ได้)"""
    doc = st.session_state.doc
    if not doc or not text.strip():
        return False
    page   = doc[st.session_state.current_page]
    x_pdf  = page.rect.width  * (x_pct / 100.0)
    y_pdf  = page.rect.height * (y_pct / 100.0)
    p      = st.session_state.current_page
    if p not in st.session_state.text_drafts:
        st.session_state.text_drafts[p] = []
    st.session_state.text_drafts[p].append({
        "text":        text,
        "x":           x_pdf,
        "y":           y_pdf,
        "font_size":   st.session_state.font_size,
        "color":       st.session_state.text_color,
        "font_name":   st.session_state.font_name,
        "font_source": st.session_state.font_source,
        "font_bytes":  st.session_state.font_bytes,
        "font_path":   st.session_state.get("font_path"),
    })
    return True

def delete_selected_text_draft():
    """ลบ text draft ที่เลือกอยู่"""
    p   = st.session_state.current_page
    idx = st.session_state.selected_text_idx
    if idx is None:
        return
    drafts = st.session_state.text_drafts.get(p, [])
    if 0 <= idx < len(drafts):
        drafts.pop(idx)
        st.session_state.text_drafts[p] = drafts
    st.session_state.selected_text_idx = None

def commit_all_drafts():
    """Commit draft objects (canvas shapes) + text drafts ทั้งหมดลง PDF จริง"""
    doc = st.session_state.doc
    if not doc:
        return False
    save_state()
    committed = 0

    # ── commit canvas shape drafts (ทุกหน้า) ──
    for pg_num, objs in st.session_state.draft_objects.items():
        if not objs:
            continue
        page = doc[pg_num]
        z    = st.session_state.zoom
        for obj in objs:
            committed += _commit_one_obj(page, obj, z)
    st.session_state.draft_objects = {}

    # ── commit text drafts (ทุกหน้า) ──
    for pg_num, texts in st.session_state.text_drafts.items():
        if not texts:
            continue
        page = doc[pg_num]
        for td in texts:
            _commit_one_text(page, td)
            committed += 1
    st.session_state.text_drafts = {}
    st.session_state.selected_text_idx = None

    st.session_state.canvas_key += 1
    return committed > 0

def _commit_one_text(page: fitz.Page, td: dict):
    """Commit text draft dict → insert_text ใน PDF"""
    color = hex_to_rgb01(td["color"])
    kwargs: dict = {"fontsize": td["font_size"], "color": color}
    src = td.get("font_source", "builtin")
    if src == "upload" and td.get("font_bytes"):
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ttf") as tmp:
            tmp.write(td["font_bytes"])
            tmp_path = tmp.name
        try:
            kwargs["fontfile"] = tmp_path
            kwargs["fontname"] = "CustomFont"
            page.insert_text(fitz.Point(td["x"], td["y"]), td["text"], **kwargs)
        finally:
            try: os.unlink(tmp_path)
            except Exception: pass
        return
    if src == "system" and td.get("font_path") and os.path.exists(td["font_path"]):
        kwargs["fontfile"] = td["font_path"]
        kwargs["fontname"] = "SystemFont"
    else:
        kwargs["fontname"] = BUILTIN_FONTS.get(td.get("font_name", "Helvetica"), "helv")
    page.insert_text(fitz.Point(td["x"], td["y"]), td["text"], **kwargs)

def _commit_one_obj(page: fitz.Page, obj: dict, z: float) -> int:
    """Commit single Fabric.js object → PyMuPDF annotation. Returns 1 if success."""
    kind   = obj.get("type", "")
    stroke_raw = obj.get("stroke", "#000000")
    fill_raw   = obj.get("fill",   "rgba(0,0,0,0)")
    c_stroke = rgba_to_rgb01(stroke_raw)
    c_fill   = rgba_to_rgb01(fill_raw)
    sw       = max(obj.get("strokeWidth", 2) / z, 0.5)
    left   = obj.get("left",  0) / z
    top    = obj.get("top",   0) / z
    sx     = obj.get("scaleX", 1.0)
    sy     = obj.get("scaleY", 1.0)
    width  = obj.get("width",  0) * sx / z
    height = obj.get("height", 0) * sy / z
    rect   = fitz.Rect(left, top, left + width, top + height)

    if kind == "rect":
        if is_highlight_obj(obj):
            if not rect.is_empty:
                annot = page.add_highlight_annot(rect.quad)
                annot.set_colors(stroke=(1, 1, 0)); annot.update(); return 1
        elif is_redact_obj(obj):
            if not rect.is_empty:
                page.add_redact_annot(rect, fill=c_fill)
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE); return 1
        else:
            if not rect.is_empty:
                annot = page.add_rect_annot(rect)
                annot.set_colors(stroke=c_stroke, fill=None)
                annot.set_border(width=sw); annot.update(); return 1

    elif kind == "circle":
        radius = obj.get("radius", 0)
        rx = radius * sx / z;  ry = radius * sy / z
        crect = fitz.Rect(left, top, left + rx*2, top + ry*2)
        if not crect.is_empty:
            annot = page.add_circle_annot(crect)
            annot.set_colors(stroke=c_stroke, fill=None)
            annot.set_border(width=sw); annot.update(); return 1

    elif kind == "line":
        x1_off = obj.get("x1", 0); y1_off = obj.get("y1", 0)
        x2_off = obj.get("x2", 0); y2_off = obj.get("y2", 0)
        bx = left + width/2;  by = top + height/2
        ax1 = bx + x1_off/z; ay1 = by + y1_off/z
        ax2 = bx + x2_off/z; ay2 = by + y2_off/z
        if abs(ax2-ax1) >= 1 or abs(ay2-ay1) >= 1:
            annot = page.add_line_annot(fitz.Point(ax1,ay1), fitz.Point(ax2,ay2))
            annot.set_colors(stroke=c_stroke); annot.set_border(width=sw)
            if obj.get("_arrow"):
                annot.set_line_ends(fitz.PDF_ANNOT_LE_NONE, fitz.PDF_ANNOT_LE_OPEN_ARROW)
            annot.update(); return 1

    elif kind == "path":
        path_cmds = obj.get("path", [])
        ox = obj.get("left", 0)/z; oy = obj.get("top", 0)/z
        points = []
        for cmd in path_cmds:
            if not cmd: continue
            op = cmd[0]
            if op in ("M","L") and len(cmd)>=3:
                points.append(fitz.Point(ox+cmd[1]/z, oy+cmd[2]/z))
            elif op=="Q" and len(cmd)>=5:
                points.append(fitz.Point(ox+cmd[3]/z, oy+cmd[4]/z))
            elif op=="C" and len(cmd)>=7:
                points.append(fitz.Point(ox+cmd[5]/z, oy+cmd[6]/z))
        if len(points) >= 2:
            annot = page.add_ink_annot([points])
            annot.set_colors(stroke=c_stroke); annot.set_border(width=sw)
            annot.update(); return 1
    return 0

# ─────────────────────────────────────────────
# Legacy add_text (kept for compatibility)
# ─────────────────────────────────────────────
def add_text(text: str, x_pct: float, y_pct: float):
    return add_text_to_draft(text, x_pct, y_pct)

# ─────────────────────────────────────────────
# Canvas → Draft store  (objects ยัง drag ได้ จนกว่าจะ Commit)
# ─────────────────────────────────────────────
def store_canvas_to_draft(json_data: dict) -> bool:
    """บันทึก objects จาก canvas เข้า draft_objects[page] (ยังไม่ลง PDF)"""
    if not json_data or not json_data.get("objects"):
        return False
    p = st.session_state.current_page
    tool = st.session_state.tool
    objs = json_data["objects"]
    # Tag line objects ว่าเป็น arrow หรือไม่
    for obj in objs:
        if obj.get("type") == "line" and tool == TOOL_ARROW:
            obj["_arrow"] = True
    st.session_state.draft_objects[p] = copy.deepcopy(objs)
    return True

# ─────────────────────────────────────────────
# Legacy commit_canvas_to_pdf (now routes through draft then commit)
# ─────────────────────────────────────────────
def commit_canvas_to_pdf(json_data: dict) -> bool:
    """compat wrapper — store draft แล้ว commit ทันที"""
    store_canvas_to_draft(json_data)
    return commit_all_drafts()

# ─────────────────────────────────────────────
# Thumbnail helper
# ─────────────────────────────────────────────
def get_thumb_image(page_idx: int, width: int = 120) -> Image.Image:
    doc  = st.session_state.doc
    page = doc[page_idx]
    r    = page.rect
    scl  = width / r.width
    mat  = fitz.Matrix(scl, scl)
    pix  = page.get_pixmap(matrix=mat, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

# ─────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────
PAGE_CSS = """
<style>
/* Sidebar */
section[data-testid="stSidebar"] > div { padding-top: 0.5rem; }
/* Thumbnail active */
.thumb-active { border: 3px solid #3498DB !important; border-radius: 4px; }
/* Tool buttons */
div.stButton > button { width: 100%; border-radius: 6px; font-size: 13px; padding: 4px 6px; }
/* Hide Streamlit header */
header[data-testid="stHeader"] { display: none; }
/* Canvas area */
.canvas-wrapper { display: flex; justify-content: center; }
</style>
"""

# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
def main():
    st.set_page_config(
        page_title="📄 PDF Annotator",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    st.markdown(PAGE_CSS, unsafe_allow_html=True)
    init_state()

    if not HAS_CANVAS:
        st.error("กรุณาติดตั้ง: `pip install streamlit-drawable-canvas`")
        return

    doc = st.session_state.doc

    # ══════════════════════════════════════════
    # SIDEBAR
    # ══════════════════════════════════════════
    with st.sidebar:
        st.markdown("## 📄 PDF Annotator")

        # ── Upload ──
        uploaded = st.file_uploader("อัปโหลดไฟล์ PDF", type=["pdf"],
                                     label_visibility="collapsed")
        if uploaded and uploaded.name != st.session_state.doc_name:
            if st.session_state.doc:
                st.session_state.doc.close()
            st.session_state.doc      = fitz.open("pdf", uploaded.read())
            st.session_state.doc_name = uploaded.name
            st.session_state.current_page = 0
            st.session_state.undo_stack.clear()
            st.session_state.redo_stack.clear()
            st.session_state.canvas_key = 0
            doc = st.session_state.doc
            st.rerun()

        if doc:
            # ── Download ──
            st.download_button(
                "📥 บันทึกไฟล์ PDF",
                data=doc.tobytes(),
                file_name=st.session_state.doc_name or "annotated.pdf",
                mime="application/pdf",
                type="primary",
                use_container_width=True
            )

            # ── Undo / Redo ──
            u_col, r_col = st.columns(2)
            with u_col:
                if st.button("↶ Undo",
                             disabled=not st.session_state.undo_stack,
                             use_container_width=True):
                    do_undo(); st.rerun()
            with r_col:
                if st.button("↷ Redo",
                             disabled=not st.session_state.redo_stack,
                             use_container_width=True):
                    do_redo(); st.rerun()

            st.divider()

            # ── Tools ──
            st.markdown("**🛠 เครื่องมือวาด**")

            def tool_btn(label, tool_id):
                is_active = st.session_state.tool == tool_id
                btn_type  = "primary" if is_active else "secondary"
                if st.button(label, key=f"tool_{tool_id}",
                             type=btn_type, use_container_width=True):
                    st.session_state.tool = tool_id
                    st.rerun()

            c1, c2 = st.columns(2)
            with c1:
                tool_btn("🖊 ปากกา",    TOOL_PEN)
                tool_btn("⬛ สี่เหลี่ยม", TOOL_RECT)
                tool_btn("➡ ลูกศร",    TOOL_ARROW)
                tool_btn("⬛ ปิดทับ",   TOOL_REDACT)
            with c2:
                tool_btn("🖱 เลือก",    TOOL_SELECT)
                tool_btn("⬭ วงรี",      TOOL_CIRCLE)
                tool_btn("╱ เส้น",      TOOL_LINE)
                tool_btn("🖍 ไฮไลท์",   TOOL_HIGHLIGHT)

            st.markdown(f"*เครื่องมือปัจจุบัน: **{st.session_state.tool}***")

            # ── Colours & width ──
            with st.expander("🎨 สีและความหนาเส้น", expanded=False):

                # helper: row = [color_picker | 💉 button]
                def color_row(label: str, key: str):
                    pc, bc = st.columns([3, 1])
                    with pc:
                        st.session_state[key] = st.color_picker(
                            label, st.session_state[key], key=f"cp_{key}")
                    with bc:
                        st.markdown("<div style='margin-top:26px'>", unsafe_allow_html=True)
                        active = st.session_state.eyedrop_mode and \
                                 st.session_state.eyedrop_target == key
                        btn_label = "💉✓" if active else "💉"
                        btn_type  = "primary" if active else "secondary"
                        if st.button(btn_label, key=f"ed_{key}",
                                     type=btn_type, use_container_width=True,
                                     help="คลิกเพื่อเปิดโหมดดูดสีจากหน้า PDF"):
                            if active:
                                st.session_state.eyedrop_mode = False
                            else:
                                st.session_state.eyedrop_mode   = True
                                st.session_state.eyedrop_target = key
                                st.session_state.canvas_key    += 1
                            st.rerun()
                        st.markdown("</div>", unsafe_allow_html=True)

                color_row("สีปากกา",         "pen_color")
                color_row("สีรูปร่าง/ลูกศร", "annot_color")
                color_row("สีกล่องปิดทับ",   "redact_color")

                if st.session_state.eyedrop_mode:
                    target_label = {
                        "pen_color":    "สีปากกา",
                        "annot_color":  "สีรูปร่าง/ลูกศร",
                        "redact_color": "สีกล่องปิดทับ",
                        "text_color":   "สีข้อความ",
                    }.get(st.session_state.eyedrop_target, "")
                    st.info(f"💉 คลิกบนหน้า PDF เพื่อดูดสีไปยัง **{target_label}**")
                    if st.session_state.eyedrop_result:
                        st.markdown(
                            f"<div style='background:{st.session_state.eyedrop_result};"
                            f"padding:6px 12px;border-radius:6px;color:#fff;"
                            f"text-shadow:0 0 3px #000;font-size:13px;'>"
                            f"สีที่ดูด: {st.session_state.eyedrop_result}</div>",
                            unsafe_allow_html=True
                        )

                st.session_state.line_width = st.slider(
                    "ความหนาเส้น", 1, 15, st.session_state.line_width)

            st.divider()

            # ── Text insertion ──
            st.markdown("**📝 เพิ่มข้อความลง PDF**")
            with st.expander("ตั้งค่าและเพิ่มข้อความ", expanded=False):
                # text colour row with eyedropper
                pc2, bc2 = st.columns([3, 1])
                with pc2:
                    st.session_state.text_color = st.color_picker(
                        "สีข้อความ", st.session_state.text_color, key="cp_text_color")
                with bc2:
                    st.markdown("<div style='margin-top:26px'>", unsafe_allow_html=True)
                    active2 = st.session_state.eyedrop_mode and \
                              st.session_state.eyedrop_target == "text_color"
                    if st.button("💉✓" if active2 else "💉",
                                 key="ed_text_color",
                                 type="primary" if active2 else "secondary",
                                 use_container_width=True,
                                 help="ดูดสีจากหน้า PDF"):
                        if active2:
                            st.session_state.eyedrop_mode = False
                        else:
                            st.session_state.eyedrop_mode   = True
                            st.session_state.eyedrop_target = "text_color"
                            st.session_state.canvas_key    += 1
                        st.rerun()
                    st.markdown("</div>", unsafe_allow_html=True)
                st.session_state.font_size  = st.slider("ขนาดตัวอักษร (pt)", 8, 72, st.session_state.font_size)

                # ── Font selector ──
                st.markdown("**🔤 เลือก Font**")
                font_src = st.radio(
                    "แหล่ง Font",
                    ["Built-in", "จากเครื่อง (System)", "อัปโหลดไฟล์ Font"],
                    horizontal=True,
                    key="font_src_radio",
                    label_visibility="collapsed",
                )

                if font_src == "Built-in":
                    st.session_state.font_source = "builtin"
                    chosen = st.selectbox(
                        "เลือก Font",
                        list(BUILTIN_FONTS.keys()),
                        index=list(BUILTIN_FONTS.keys()).index(
                            st.session_state.font_name)
                        if st.session_state.font_name in BUILTIN_FONTS else 0,
                        key="builtin_font_sel",
                    )
                    st.session_state.font_name = chosen
                    # preview
                    prev_uri = render_font_preview(None, None, chosen)
                    if prev_uri:
                        st.markdown(
                            f"<img src='{prev_uri}' style='width:100%;border:1px solid #ccc;"
                            f"border-radius:4px;margin-top:4px;'>",
                            unsafe_allow_html=True,
                        )

                elif font_src == "จากเครื่อง (System)":
                    st.session_state.font_source = "system"
                    sys_fonts = scan_system_fonts()
                    if sys_fonts:
                        sorted_names = sorted(sys_fonts.keys())
                        prev_sys = st.session_state.get("_sys_font_sel", sorted_names[0])
                        chosen_sys = st.selectbox(
                            f"Font บนเครื่อง ({len(sys_fonts)} ตัว)",
                            sorted_names,
                            index=sorted_names.index(prev_sys)
                                  if prev_sys in sorted_names else 0,
                            key="sys_font_sel",
                        )
                        st.session_state._sys_font_sel = chosen_sys
                        st.session_state.font_name  = chosen_sys
                        st.session_state.font_path  = sys_fonts[chosen_sys]
                        # preview
                        prev_uri2 = render_font_preview(
                            None, sys_fonts[chosen_sys], chosen_sys)
                        if prev_uri2:
                            st.markdown(
                                f"<img src='{prev_uri2}' style='width:100%;border:1px solid #ccc;"
                                f"border-radius:4px;margin-top:4px;'>",
                                unsafe_allow_html=True,
                            )
                        st.caption(f"📂 {sys_fonts[chosen_sys]}")
                    else:
                        st.warning("ไม่พบ Font บนเครื่อง — ลองใช้ Built-in หรืออัปโหลดไฟล์")

                else:  # อัปโหลดไฟล์
                    st.session_state.font_source = "upload"
                    uploaded_font = st.file_uploader(
                        "เลือกไฟล์ Font (.ttf / .otf)",
                        type=["ttf", "otf", "TTF", "OTF"],
                        key="font_uploader",
                        help="รองรับ TrueType (.ttf) และ OpenType (.otf)"
                    )
                    if uploaded_font:
                        fbytes = uploaded_font.read()
                        st.session_state.font_bytes = fbytes
                        st.session_state.font_name  = os.path.splitext(
                            uploaded_font.name)[0]
                        # preview
                        prev_uri3 = render_font_preview(fbytes, None,
                                                        st.session_state.font_name)
                        if prev_uri3:
                            st.markdown(
                                f"<img src='{prev_uri3}' style='width:100%;border:1px solid #ccc;"
                                f"border-radius:4px;margin-top:4px;'>",
                                unsafe_allow_html=True,
                            )
                        st.success(f"✅ โหลด Font: **{st.session_state.font_name}**")
                    elif st.session_state.font_bytes:
                        st.info(f"Font ปัจจุบัน: **{st.session_state.font_name}**")

                # แสดง badge font ที่เลือก
                st.markdown(
                    f"<div style='background:#eaf4fb;border-left:4px solid #3498db;"
                    f"padding:4px 10px;border-radius:4px;font-size:12px;margin:6px 0;'>"
                    f"🔤 <b>{st.session_state.font_name}</b> "
                    f"<span style='color:#888;'>({st.session_state.font_source})</span></div>",
                    unsafe_allow_html=True,
                )

                txt = st.text_area("ข้อความ", key="txt_input", height=80)
                c_x, c_y = st.columns(2)
                with c_x: xp = st.slider("X (%)", 0, 100, 10, key="txt_x")
                with c_y: yp = st.slider("Y (%)", 0, 100, 20, key="txt_y")
                if st.button("➕ เพิ่มข้อความ (Draft)", type="primary", use_container_width=True):
                    if add_text_to_draft(txt, xp, yp):
                        st.success("เพิ่มข้อความใน Draft แล้ว — ลากปรับตำแหน่งได้")
                        st.rerun()
                    else:
                        st.warning("กรุณาพิมพ์ข้อความก่อน")

            # ── Text draft list (แสดงข้อความที่รอ commit) ──
            p = st.session_state.current_page
            text_drafts_page = st.session_state.text_drafts.get(p, [])
            if text_drafts_page:
                st.markdown("**✏️ ข้อความ Draft (ลากปรับตำแหน่งได้)**")
                for idx, td in enumerate(text_drafts_page):
                    is_sel = (st.session_state.selected_text_idx == idx)
                    bg_col = "#d6eaf8" if is_sel else "#f8f9fa"
                    preview = td["text"][:25] + ("…" if len(td["text"]) > 25 else "")
                    st.markdown(
                        f"<div style='background:{bg_col};border:1px solid #bdc3c7;"
                        f"border-radius:6px;padding:4px 8px;margin:2px 0;font-size:12px;"
                        f"cursor:pointer;'>"
                        f"<b>#{idx+1}</b> {preview} "
                        f"<span style='color:#888;font-size:10px;'>"
                        f"({td['font_name'][:12]}, {td['font_size']}pt)</span></div>",
                        unsafe_allow_html=True,
                    )
                    sel_col, del_col = st.columns([3, 1])
                    with sel_col:
                        if st.button(f"🖱 เลือก #{idx+1}", key=f"sel_td_{idx}",
                                     use_container_width=True,
                                     type="primary" if is_sel else "secondary"):
                            st.session_state.selected_text_idx = None if is_sel else idx
                            st.rerun()
                    with del_col:
                        if st.button("🗑", key=f"del_td_{idx}", use_container_width=True):
                            text_drafts_page.pop(idx)
                            st.session_state.text_drafts[p] = text_drafts_page
                            if st.session_state.selected_text_idx == idx:
                                st.session_state.selected_text_idx = None
                            st.rerun()

                # Nudge ปรับตำแหน่งข้อความที่เลือก
                sel_idx = st.session_state.selected_text_idx
                if sel_idx is not None and 0 <= sel_idx < len(text_drafts_page):
                    td_sel = text_drafts_page[sel_idx]
                    doc_cur = st.session_state.doc
                    if doc_cur:
                        pg_rect = doc_cur[p].rect
                        st.markdown(f"**📐 ปรับตำแหน่ง #{sel_idx+1}**")
                        new_x_pct = st.slider("X (%)", 0, 100,
                                              int(td_sel["x"] / pg_rect.width * 100),
                                              key=f"nudge_x_{sel_idx}")
                        new_y_pct = st.slider("Y (%)", 0, 100,
                                              int(td_sel["y"] / pg_rect.height * 100),
                                              key=f"nudge_y_{sel_idx}")
                        td_sel["x"] = pg_rect.width  * (new_x_pct / 100.0)
                        td_sel["y"] = pg_rect.height * (new_y_pct / 100.0)
                        text_drafts_page[sel_idx] = td_sel
                        st.session_state.text_drafts[p] = text_drafts_page

            st.divider()

            # ── Page management ──
            st.markdown("**📄 จัดการหน้าเอกสาร**")
            pg_col1, pg_col2 = st.columns(2)
            with pg_col1:
                if st.button("🗑 ลบหน้านี้", use_container_width=True):
                    delete_current_page(); st.rerun()
                if st.button("↻ หมุน 90°", use_container_width=True):
                    rotate_current_page(90); st.rerun()
            with pg_col2:
                if st.button("+ หน้าว่าง", use_container_width=True):
                    insert_blank_page(); st.rerun()
                if st.button("↻ หมุน 180°", use_container_width=True):
                    rotate_current_page(180); st.rerun()

            # แทรก PDF จากไฟล์
            with st.expander("📎 แทรกไฟล์ PDF อื่น", expanded=False):
                src_pdf = st.file_uploader("เลือกไฟล์ PDF ที่ต้องการแทรก",
                                            type=["pdf"], key="insert_pdf")
                if src_pdf and st.button("แทรกหลังหน้าปัจจุบัน", use_container_width=True):
                    save_state()
                    src_doc = fitz.open("pdf", src_pdf.read())
                    after   = st.session_state.current_page
                    for i in range(len(src_doc)):
                        doc.insert_pdf(src_doc, from_page=i, to_page=i,
                                       start_at=after + i + 1)
                    src_doc.close()
                    st.session_state.canvas_key += 1
                    st.success(f"แทรก {len(src_doc)} หน้าสำเร็จ!")
                    st.rerun()

            st.divider()

            # ── Thumbnails ──
            st.markdown("**🖼 หน้าเอกสาร**")
            n = total_pages()
            for i in range(n):
                is_cur = (i == st.session_state.current_page)
                try:
                    thumb = get_thumb_image(i, width=140)
                    caption = f"หน้า {i+1}" + (" ◀" if is_cur else "")
                    # แสดง thumbnail พร้อม border ถ้าเป็นหน้าปัจจุบัน
                    if is_cur:
                        st.markdown(f"<div style='border:3px solid #3498DB;border-radius:4px;margin:2px 0;'>",
                                    unsafe_allow_html=True)
                    st.image(thumb, caption=caption, use_container_width=True)
                    if is_cur:
                        st.markdown("</div>", unsafe_allow_html=True)
                    if st.button(f"ไปหน้า {i+1}", key=f"pg_{i}",
                                 use_container_width=True,
                                 type="primary" if is_cur else "secondary"):
                        go_page(i); st.rerun()
                except Exception:
                    if st.button(f"หน้า {i+1}", key=f"pg_{i}",
                                 use_container_width=True):
                        go_page(i); st.rerun()

    # ══════════════════════════════════════════
    # MAIN CONTENT
    # ══════════════════════════════════════════
    # ── Main Content ──
    if not st.session_state.doc:
        st.info("👉 กรุณาอัปโหลดไฟล์ PDF ด้านล่างนี้เพื่อเริ่มต้นทำงาน")
        
        # เพิ่มกล่องอัปโหลดไว้ตรงกลางหน้าจอไปเลย
        uploaded_file_main = st.file_uploader("อัปโหลดไฟล์ PDF (ลากไฟล์มาวางตรงนี้ได้เลย)", type=['pdf'])
        
        if uploaded_file_main and uploaded_file_main.name != st.session_state.current_file:
            st.session_state.doc = fitz.open(stream=file_bytes, filetype="pdf")
            st.session_state.current_file = uploaded_file_main.name
            st.session_state.current_page = 0
            st.session_state.undo_stack.clear()
            st.session_state.canvas_key = 0
            st.rerun()
            
        return

    # ── Navigation bar ──
    nav1, nav2, nav3, nav4, nav5, nav6, nav7 = st.columns([1.2, 1.2, 2, 1.2, 1.2, 1.5, 2])
    with nav1:
        if st.button("◀ ก่อนหน้า", use_container_width=True):
            if st.session_state.current_page > 0:
                st.session_state.current_page -= 1
                st.session_state.canvas_key   += 1
                st.rerun()
    with nav2:
        if st.button("ถัดไป ▶", use_container_width=True):
            if st.session_state.current_page < total_pages() - 1:
                st.session_state.current_page += 1
                st.session_state.canvas_key   += 1
                st.rerun()
    with nav3:
        st.markdown(
            f"<div style='text-align:center;padding-top:6px;font-weight:bold;font-size:15px;'>"
            f"หน้า {st.session_state.current_page+1} / {total_pages()}</div>",
            unsafe_allow_html=True
        )
    with nav4:
        if st.button("🔍+ ซูมเข้า", use_container_width=True):
            st.session_state.zoom = min(st.session_state.zoom + ZOOM_STEP, ZOOM_MAX)
            st.rerun()
    with nav5:
        if st.button("🔍- ซูมออก", use_container_width=True):
            st.session_state.zoom = max(st.session_state.zoom - ZOOM_STEP, ZOOM_MIN)
            st.rerun()
    with nav6:
        st.markdown(
            f"<div style='text-align:center;padding-top:6px;font-size:14px;'>"
            f"ซูม: {int(st.session_state.zoom*100)}%</div>",
            unsafe_allow_html=True
        )
    with nav7:
        if st.button("🗑 ลบ Annotation ทั้งหน้า", use_container_width=True):
            delete_all_annots(); st.rerun()

    st.markdown("---")

    # ── Canvas setup ──
    bg = get_page_image()
    if bg is None:
        st.error("ไม่สามารถโหลดหน้า PDF ได้")
        return
    # --- เพิ่มโค้ดนี้เพื่อบังคับแสดงภาพดิบๆ ---
    if bg_image:
        # บังคับลดขนาดรูปถ้ามันใหญ่เกินไป (ป้องกัน Canvas จอดำ/ขาว)
        if bg_image.width > 2000 or bg_image.height > 2000:
            st.warning("⚠️ ไฟล์ภาพมีขนาดใหญ่เกินไป ระบบกำลังปรับลดขนาดอัตโนมัติ...")
            bg_image.thumbnail((1500, 1500), Image.Resampling.LANCZOS)
    else:
        st.error("❌ ไม่สามารถดึงภาพจากไฟล์ PDF ได้")
    # -------------------------------------

    # วาด text drafts ลงบน bg image เพื่อแสดงตัวอย่าง (ก่อน commit)
    p = st.session_state.current_page
    text_drafts_page = st.session_state.text_drafts.get(p, [])
    if text_drafts_page:
        from PIL import ImageDraw as _IDraw, ImageFont as _IFont
        import io as _io
        bg_draw = bg.copy()
        draw    = _IDraw.Draw(bg_draw)
        sel_idx = st.session_state.selected_text_idx
        for i, td in enumerate(text_drafts_page):
            cx = int(td["x"] * st.session_state.zoom)
            cy = int(td["y"] * st.session_state.zoom)
            fs = int(td["font_size"] * st.session_state.zoom)
            try:
                r, g, b = hex_to_rgb01(td["color"])
                fill_c  = (int(r*255), int(g*255), int(b*255))
            except Exception:
                fill_c = (0, 0, 0)
            # โหลด font
            fnt = None
            try:
                if td.get("font_source") == "upload" and td.get("font_bytes"):
                    fnt = _IFont.truetype(_io.BytesIO(td["font_bytes"]), fs)
                elif td.get("font_source") == "system" and td.get("font_path"):
                    fnt = _IFont.truetype(td["font_path"], fs)
            except Exception:
                fnt = None
            if fnt is None:
                try: fnt = _IFont.load_default(size=fs)
                except Exception: fnt = _IFont.load_default()
            # วาด selection box
            if i == sel_idx:
                bbox = draw.textbbox((cx, cy), td["text"], font=fnt)
                draw.rectangle([bbox[0]-3, bbox[1]-3, bbox[2]+3, bbox[3]+3],
                               outline="#3498db", width=2)
                draw.rectangle([bbox[0]-4, bbox[1]-4, bbox[2]+4, bbox[3]+4],
                               outline="#aed6f1", width=1)
            draw.text((cx, cy), td["text"], font=fnt, fill=fill_c)
        bg = bg_draw

    tool = st.session_state.tool
    canvas_mode  = "freedraw"
    stroke_color = st.session_state.annot_color
    fill_color   = "rgba(0,0,0,0)"

    if tool == TOOL_SELECT:
        canvas_mode = "transform"
    elif tool == TOOL_PEN:
        canvas_mode  = "freedraw"
        stroke_color = st.session_state.pen_color
    elif tool == TOOL_RECT:
        canvas_mode = "rect"
    elif tool == TOOL_CIRCLE:
        canvas_mode = "circle"
    elif tool in (TOOL_LINE, TOOL_ARROW):
        canvas_mode = "line"
    elif tool == TOOL_HIGHLIGHT:
        canvas_mode  = "rect"
        stroke_color = HL_STROKE
        fill_color   = HL_FILL
    elif tool == TOOL_REDACT:
        canvas_mode  = "rect"
        stroke_color = st.session_state.redact_color
        fill_color   = st.session_state.redact_color

    # ── สร้าง initial_drawing จาก draft objects ──
    draft_objs = st.session_state.draft_objects.get(p, [])
    initial_drawing = {"version": "4.6.0", "objects": draft_objs} if draft_objs else None

    # ── Eyedrop mode overrides canvas ──
    eyedrop_active = st.session_state.eyedrop_mode
    if eyedrop_active:
        target_label = {
            "pen_color":    "สีปากกา",
            "annot_color":  "สีรูปร่าง/ลูกศร",
            "redact_color": "สีกล่องปิดทับ",
            "text_color":   "สีข้อความ",
        }.get(st.session_state.eyedrop_target, "?")
        st.info(f"💉 **โหมดดูดสี** — คลิกบนหน้า PDF เพื่อดูดสีไปยัง **{target_label}**  "
                f"*(กด ✖ ยกเลิก หรือคลิก 💉 ใน Sidebar อีกครั้ง)*")
        col_cancel, _ = st.columns([1, 4])
        with col_cancel:
            if st.button("✖ ยกเลิกดูดสี", use_container_width=True):
                st.session_state.eyedrop_mode = False
                st.session_state.canvas_key  += 1
                st.rerun()

    # แสดง info เมื่อมี draft objects
    total_drafts = len(draft_objs) + len(text_drafts_page)
    if total_drafts > 0 and not eyedrop_active:
        st.info(
            f"📋 มี **{total_drafts}** object ใน Draft "
            f"({'รูปร่าง: ' + str(len(draft_objs)) if draft_objs else ''}"
            f"{', ' if draft_objs and text_drafts_page else ''}"
            f"{'ข้อความ: ' + str(len(text_drafts_page)) if text_drafts_page else ''})"
            f" — ใช้เครื่องมือ **🖱 เลือก** เพื่อลากปรับตำแหน่ง  "
            f"กด **✅ Commit** เพื่อฝังลง PDF"
        )

    st.markdown("<div class='canvas-wrapper'>", unsafe_allow_html=True)

    # ── ถ้าอยู่ใน eyedrop mode ใช้ "point" mode ──
    if eyedrop_active:
        eyedrop_result = st_canvas(
            fill_color       = "rgba(255,100,0,0.5)",
            stroke_width     = 6,
            stroke_color     = "#ff6400",
            background_image = bg,
            update_streamlit = True,
            height           = bg.height,
            width            = bg.width,
            drawing_mode     = "point",
            point_display_radius = 5,
            key              = f"eyedrop_{p}_{st.session_state.canvas_key}",
            display_toolbar  = False,
        )
        if (eyedrop_result is not None and
                eyedrop_result.json_data and
                eyedrop_result.json_data.get("objects")):
            obj = eyedrop_result.json_data["objects"][-1]
            cx  = int(obj.get("left", 0))
            cy  = int(obj.get("top",  0))
            sampled = sample_color_from_page(bg, cx, cy, radius=3)
            target  = st.session_state.eyedrop_target
            st.session_state[target]        = sampled
            st.session_state.eyedrop_result = sampled
            st.session_state.eyedrop_mode   = False
            st.session_state.canvas_key    += 1
            st.success(f"💉 ดูดสี {sampled} ไปยัง {target_label} สำเร็จ!")
            st.rerun()
    else:
        # ── SELECT mode: update_streamlit=True เพื่อจับ drag ──
        need_live = (canvas_mode == "transform")
        canvas_result = st_canvas(
            fill_color       = fill_color,
            stroke_width     = st.session_state.line_width,
            stroke_color     = stroke_color,
            background_image = bg,
            update_streamlit = need_live,
            height           = bg.height,
            width            = bg.width,
            drawing_mode     = canvas_mode,
            initial_drawing  = initial_drawing,
            key              = f"canvas_{p}_{st.session_state.canvas_key}",
            display_toolbar  = True,
        )
        # ── จับ canvas state ทุกครั้งที่มีการเปลี่ยนแปลง ──
        if canvas_result is not None and canvas_result.json_data:
            objs = canvas_result.json_data.get("objects", [])
            if objs:
                # Tag arrow
                for obj in objs:
                    if obj.get("type") == "line" and tool == TOOL_ARROW:
                        obj["_arrow"] = True
                st.session_state.draft_objects[p] = copy.deepcopy(objs)

    st.markdown("</div>", unsafe_allow_html=True)

    # ── Commit + Clear draft buttons ──
    if not eyedrop_active:
        b1, b2 = st.columns([3, 1])
        with b1:
            if st.button("✅ Commit — ฝัง Draft ทั้งหมดลง PDF",
                         type="primary", use_container_width=True):
                if commit_all_drafts():
                    st.success("✅ Commit สำเร็จ! ฝัง object ทั้งหมดลง PDF แล้ว")
                    st.rerun()
                else:
                    st.warning("⚠️ ไม่มี Draft object — กรุณาวาดหรือเพิ่มข้อความก่อน")
        with b2:
            if st.button("🗑 ล้าง Draft", use_container_width=True):
                st.session_state.draft_objects.pop(p, None)
                st.session_state.text_drafts.pop(p, None)
                st.session_state.selected_text_idx = None
                st.session_state.canvas_key += 1
                st.rerun()

    # ── Info footer ──
    with st.expander("ℹ️ วิธีใช้งาน", expanded=False):
        st.markdown("""
**การวาด:**
- เลือกเครื่องมือจาก Sidebar → วาดบน canvas → กด **Commit** เพื่อบันทึกลง PDF
- **ปากกา** — ลากวาดอิสระ  
- **สี่เหลี่ยม / วงรี** — คลิกลากเพื่อสร้างรูปร่าง  
- **เส้น / ลูกศร** — คลิกจุดเริ่ม → ลากไปจุดสิ้นสุด  
- **ไฮไลท์** — ลากคลุมข้อความ (สีเหลืองโปร่งใส)  
- **ปิดทับ (Redact)** — ลากสร้างกล่องทึบปิดข้อความ  
- **เพิ่มข้อความ** — พิมพ์ใน Sidebar แล้วกด ➕  

**💉 ดูดสี (Eyedropper):**
- กดปุ่ม **💉** ข้างช่องสีที่ต้องการ → คลิกบนพื้นที่ PDF  
- โปรแกรมจะดูดสีเฉลี่ยรอบจุดนั้น (radius 3 px) มาใส่ช่องสีโดยอัตโนมัติ  

**Undo / Redo:**
- กด **↶ Undo** / **↷ Redo** ใน Sidebar  

**จัดการหน้า:**
- ลบ / แทรกหน้าว่าง / หมุน / แทรก PDF จาก Sidebar  

**บันทึก:**
- กด **📥 บันทึกไฟล์ PDF** เพื่อ download ไฟล์ที่แก้ไขแล้ว
        """)


if __name__ == "__main__":
    main()