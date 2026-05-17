"""
PDF Annotator — Streamlit  (Draggable Objects Edition)
======================================================
สถาปัตยกรรม "Pending Layer":
  • วัตถุทุกชิ้น (รูปร่าง, ข้อความ, ลูกศร ฯลฯ) ถูกเก็บใน session_state["draft_objects"][page]
    เป็น Fabric.js JSON ก่อน  — ยังไม่ฝังลง PDF
  • Canvas แสดงผล PDF เป็น background + โหลด initialData จาก draft ทำให้
    วัตถุทุกชิ้นสามารถ คลิกเลือก / ลาก / resize ได้ใน "transform" mode
  • กด Commit → แปลง draft ทั้งหน้าเป็น annotation ใน PDF จริง แล้วล้าง draft
  • Text Overlay มีระบบแยก: เก็บเป็น text_drafts, render บน canvas ด้วย Fabric textbox,
    ลาก/ย้ายได้, font file ฝังตอน commit
"""

import streamlit as st
import fitz
from PIL import Image
import copy, io, os, json, base64, tempfile

try:
    from streamlit_drawable_canvas import st_canvas
    HAS_CANVAS = True
except ImportError:
    HAS_CANVAS = False

# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────
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

BUILTIN_FONTS = {
    "Helvetica":              "helv",
    "Helvetica Bold":         "hebo",
    "Helvetica Oblique":      "heio",
    "Times Roman":            "tiro",
    "Times Bold":             "tibo",
    "Times Italic":           "tiit",
    "Times Bold Italic":      "tibi",
    "Courier":                "cour",
    "Courier Bold":           "cobo",
    "Courier Oblique":        "coit",
    "Symbol":                 "symb",
}

# ──────────────────────────────────────────────────────────────
# Colour helpers
# ──────────────────────────────────────────────────────────────
def hex_to_rgb01(h: str):
    h = h.lstrip("#")
    return int(h[0:2],16)/255, int(h[2:4],16)/255, int(h[4:6],16)/255

def rgb_to_hex(r,g,b):
    return "#{:02x}{:02x}{:02x}".format(int(r),int(g),int(b))

def rgba_to_rgb01(s: str):
    s = s.strip()
    if s.startswith("#"):
        return hex_to_rgb01(s)
    try:
        v = s.replace("rgba(","").replace("rgb(","").replace(")","").split(",")
        return int(v[0])/255, int(v[1])/255, int(v[2])/255
    except Exception:
        return 0.0, 0.0, 0.0

def sample_color_from_page(img: Image.Image, cx: int, cy: int, radius=3) -> str:
    arr = img.convert("RGB"); w,h = arr.size
    x0,x1 = max(0,cx-radius), min(w,cx+radius+1)
    y0,y1 = max(0,cy-radius), min(h,cy+radius+1)
    px = [arr.getpixel((x,y)) for x in range(x0,x1) for y in range(y0,y1)]
    if not px: return "#000000"
    return rgb_to_hex(sum(p[0] for p in px)//len(px),
                      sum(p[1] for p in px)//len(px),
                      sum(p[2] for p in px)//len(px))

def is_highlight_color(s: str) -> bool:
    s = s.replace(" ","")
    return "255,255,0" in s

def is_transparent(s: str) -> bool:
    s = s.strip().replace(" ","")
    if s in ("", "rgba(0,0,0,0)", "transparent"): return True
    if s.startswith("rgba"):
        try:
            return float(s.replace("rgba(","").replace(")","").split(",")[3]) < 0.05
        except Exception: pass
    return False

# ──────────────────────────────────────────────────────────────
# Font helpers
# ──────────────────────────────────────────────────────────────
def scan_system_fonts() -> dict:
    import platform, glob
    dirs = []
    sys_name = platform.system()
    if sys_name == "Windows":
        dirs = [r"C:\Windows\Fonts"]
    elif sys_name == "Darwin":
        dirs = ["/Library/Fonts","/System/Library/Fonts",
                os.path.expanduser("~/Library/Fonts")]
    else:
        dirs = ["/usr/share/fonts","/usr/local/share/fonts",
                os.path.expanduser("~/.fonts"),
                os.path.expanduser("~/.local/share/fonts")]
    fonts = {}
    for d in dirs:
        for ext in ("*.ttf","*.otf","*.TTF","*.OTF"):
            for path in glob.glob(os.path.join(d,"**",ext), recursive=True):
                name = os.path.splitext(os.path.basename(path))[0]
                fonts[name] = path
    return fonts

def render_font_preview(font_bytes=None, font_path=None, size=20) -> str:
    try:
        from PIL import ImageFont, ImageDraw
        img  = Image.new("RGB",(460,48),"#fafafa")
        draw = ImageDraw.Draw(img)
        try:
            if font_bytes:
                fnt = ImageFont.truetype(io.BytesIO(font_bytes), size)
            elif font_path and os.path.exists(font_path):
                fnt = ImageFont.truetype(font_path, size)
            else:
                fnt = ImageFont.load_default()
        except Exception:
            fnt = ImageFont.load_default()
        draw.text((6,6),"AaBbCc 0123 ภาษาไทย",font=fnt,fill="#1a1a2e")
        buf = io.BytesIO(); img.save(buf,"PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/png;base64,{b64}"
    except Exception:
        return ""

# ──────────────────────────────────────────────────────────────
# Session state
# ──────────────────────────────────────────────────────────────
def init_state():
    defs = {
        "doc": None, "doc_name": "", "current_page": 0,
        "zoom": 1.0, "tool": TOOL_PEN,
        "pen_color": "#c0392b", "annot_color": "#f1c40f",
        "text_color": "#1a5276", "redact_color": "#000000",
        "line_width": 2, "font_size": 14,
        "undo_stack": [], "redo_stack": [], "canvas_key": 0,
        "eyedrop_mode": False, "eyedrop_target": "pen_color",
        "eyedrop_result": "",
        "font_bytes": None, "font_name": "Helvetica",
        "font_source": "builtin", "font_path": None,
        # Pending layer — Fabric.js object list per page
        "draft_objects": {},   # {page_no: [fabric_obj, ...]}
    }
    for k,v in defs.items():
        if k not in st.session_state:
            st.session_state[k] = v

# ──────────────────────────────────────────────────────────────
# Undo / Redo
# ──────────────────────────────────────────────────────────────
def _snapshot():
    doc = st.session_state.doc
    if not doc: return None
    return {
        "pdf_bytes":     doc.tobytes(),
        "current_page":  st.session_state.current_page,
        "draft_objects": copy.deepcopy(st.session_state.draft_objects),
    }

def save_state():
    s = _snapshot()
    if not s: return
    st.session_state.undo_stack.append(s)
    if len(st.session_state.undo_stack) > MAX_UNDO:
        st.session_state.undo_stack.pop(0)
    st.session_state.redo_stack.clear()

def do_undo():
    if not st.session_state.undo_stack: return
    cur = _snapshot()
    if cur: st.session_state.redo_stack.append(cur)
    _restore(st.session_state.undo_stack.pop())

def do_redo():
    if not st.session_state.redo_stack: return
    cur = _snapshot()
    if cur: st.session_state.undo_stack.append(cur)
    _restore(st.session_state.redo_stack.pop())

def _restore(snap):
    if st.session_state.doc: st.session_state.doc.close()
    st.session_state.doc           = fitz.open("pdf", snap["pdf_bytes"])
    st.session_state.current_page  = snap["current_page"]
    st.session_state.draft_objects = copy.deepcopy(snap.get("draft_objects",{}))
    st.session_state.canvas_key   += 1

# ──────────────────────────────────────────────────────────────
# PDF / page helpers
# ──────────────────────────────────────────────────────────────
def get_page_image() -> Image.Image | None:
    doc = st.session_state.doc
    if not doc: return None
    pix = doc[st.session_state.current_page].get_pixmap(
        matrix=fitz.Matrix(st.session_state.zoom, st.session_state.zoom),
        alpha=False)
    return Image.frombytes("RGB",(pix.width,pix.height),pix.samples)

def total_pages() -> int:
    return len(st.session_state.doc) if st.session_state.doc else 0

def go_page(n):
    if st.session_state.doc and 0 <= n < total_pages():
        st.session_state.current_page = n
        st.session_state.canvas_key  += 1

def delete_current_page():
    doc = st.session_state.doc
    if not doc or len(doc) <= 1:
        st.warning("ต้องมีอย่างน้อย 1 หน้า"); return
    save_state()
    p = st.session_state.current_page
    doc.delete_page(p)
    new_draft = {}
    for k,v in st.session_state.draft_objects.items():
        if k < p: new_draft[k] = v
        elif k > p: new_draft[k-1] = v
    st.session_state.draft_objects = new_draft
    st.session_state.current_page = min(p, len(doc)-1)
    st.session_state.canvas_key += 1

def insert_blank_page():
    doc = st.session_state.doc
    if not doc: return
    save_state()
    after = st.session_state.current_page
    doc.new_page(width=595, height=842, after=after)
    new_draft = {}
    for k,v in st.session_state.draft_objects.items():
        new_draft[k if k <= after else k+1] = v
    st.session_state.draft_objects = new_draft
    st.session_state.canvas_key += 1

def rotate_current_page(angle=90):
    doc = st.session_state.doc
    if not doc: return
    save_state()
    page = doc[st.session_state.current_page]
    page.set_rotation((page.rotation + angle) % 360)
    st.session_state.canvas_key += 1

def delete_all_annots():
    doc = st.session_state.doc
    if not doc: return
    page = doc[st.session_state.current_page]
    annots = list(page.annots())
    if annots:
        save_state()
        for a in annots: page.delete_annot(a)
    p = st.session_state.current_page
    st.session_state.draft_objects.pop(p, None)
    st.session_state.canvas_key += 1

def get_thumb(idx, w=130) -> Image.Image:
    doc = st.session_state.doc
    page = doc[idx]
    s = w / page.rect.width
    pix = page.get_pixmap(matrix=fitz.Matrix(s,s), alpha=False)
    return Image.frombytes("RGB",(pix.width,pix.height),pix.samples)

# ──────────────────────────────────────────────────────────────
# Draft / pending layer
# ──────────────────────────────────────────────────────────────
def current_draft() -> list:
    p = st.session_state.current_page
    return st.session_state.draft_objects.get(p, [])

def set_draft(objects: list):
    p = st.session_state.current_page
    st.session_state.draft_objects[p] = objects

def draft_as_initial_json() -> dict | None:
    objs = current_draft()
    if not objs: return None
    return {"version": "4.6.0", "objects": objs}

def sync_draft_from_canvas(json_data: dict):
    """รับ canvas json → update draft (รองรับ drag/resize)"""
    if not json_data: return
    objects = json_data.get("objects") or []
    set_draft(objects)

# ──────────────────────────────────────────────────────────────
# Add objects to draft
# ──────────────────────────────────────────────────────────────
def add_text_to_draft(text: str, x_pct: float, y_pct: float) -> bool:
    doc = st.session_state.doc
    if not doc or not text.strip(): return False
    save_state()
    page = doc[st.session_state.current_page]
    z    = st.session_state.zoom
    cx   = page.rect.width  * (x_pct/100.0) * z
    cy   = page.rect.height * (y_pct/100.0) * z
    fs   = int(st.session_state.font_size * z)

    src    = st.session_state.get("font_source","builtin")
    fname  = st.session_state.get("font_name","Helvetica")
    fpath  = st.session_state.get("font_path")
    fbytes = st.session_state.get("font_bytes")
    fb64   = base64.b64encode(fbytes).decode() if fbytes else None

    obj = {
        "type": "textbox",
        "text": text,
        "left": cx, "top": cy,
        "width": max(len(text) * fs * 0.65, 80),
        "fontSize": fs,
        "fill": st.session_state.text_color,
        "fontFamily": fname,
        "scaleX": 1, "scaleY": 1,
        "selectable": True, "evented": True, "editable": True,
        # metadata สำหรับ commit
        "_font_source": src,
        "_font_name":   fname,
        "_font_path":   fpath,
        "_font_bytes":  fb64,
    }
    drafts = current_draft()
    drafts.append(obj)
    set_draft(drafts)
    st.session_state.canvas_key += 1
    return True

# ──────────────────────────────────────────────────────────────
# Commit draft → PDF
# ──────────────────────────────────────────────────────────────
def commit_draft_to_pdf() -> int:
    doc    = st.session_state.doc
    drafts = current_draft()
    if not doc or not drafts: return 0
    save_state()
    page = doc[st.session_state.current_page]
    z    = st.session_state.zoom
    committed = 0

    for obj in drafts:
        kind = obj.get("type","")
        left = obj.get("left",0) / z
        top  = obj.get("top",0)  / z
        sx   = obj.get("scaleX",1.0)
        sy   = obj.get("scaleY",1.0)
        w    = obj.get("width",0)  * sx / z
        h    = obj.get("height",0) * sy / z
        stroke_raw = obj.get("stroke","#000000")
        fill_raw   = obj.get("fill","rgba(0,0,0,0)")
        sw         = max(obj.get("strokeWidth",2)/z, 0.5)
        c_stroke   = rgba_to_rgb01(stroke_raw)
        c_fill     = rgba_to_rgb01(fill_raw)
        meta_kind  = obj.get("_kind","")

        try:
            # ── text / textbox ──
            if kind in ("text","textbox","i-text"):
                text    = obj.get("text","")
                fs      = obj.get("fontSize",14) / z
                col_raw = obj.get("fill","#000000")
                color   = (hex_to_rgb01(col_raw) if col_raw.startswith("#")
                           else rgba_to_rgb01(col_raw))
                kw: dict = {"fontsize": max(fs,6), "color": color}

                fsrc   = obj.get("_font_source","builtin")
                fname  = obj.get("_font_name","Helvetica")
                fpath  = obj.get("_font_path")
                fb64   = obj.get("_font_bytes")
                fbytes = base64.b64decode(fb64) if fb64 else None

                tmp_path = None
                if fsrc == "upload" and fbytes:
                    tmp = tempfile.NamedTemporaryFile(delete=False,suffix=".ttf")
                    tmp.write(fbytes); tmp.close()
                    tmp_path = tmp.name
                    kw["fontfile"] = tmp_path
                    kw["fontname"] = "CustomFont"
                elif fsrc == "system" and fpath and os.path.exists(fpath):
                    kw["fontfile"] = fpath
                    kw["fontname"] = "SystemFont"
                else:
                    kw["fontname"] = BUILTIN_FONTS.get(fname,"helv")

                page.insert_text(fitz.Point(left,top), text, **kw)
                if tmp_path:
                    try: os.unlink(tmp_path)
                    except Exception: pass
                committed += 1

            # ── rect / highlight / redact ──
            elif kind == "rect":
                rect = fitz.Rect(left,top,left+w,top+h)
                if rect.is_empty: continue
                if meta_kind=="highlight" or is_highlight_color(fill_raw):
                    ann = page.add_highlight_annot(rect.quad)
                    ann.set_colors(stroke=(1,1,0)); ann.update()
                elif (meta_kind=="redact" or
                      (not is_transparent(fill_raw) and
                       not is_highlight_color(fill_raw))):
                    page.add_redact_annot(rect,fill=c_fill)
                    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
                else:
                    ann = page.add_rect_annot(rect)
                    ann.set_colors(stroke=c_stroke,fill=None)
                    ann.set_border(width=sw); ann.update()
                committed += 1

            # ── ellipse ──
            elif kind in ("ellipse","circle"):
                rx = obj.get("rx", obj.get("radius",0)) * sx / z
                ry = obj.get("ry", obj.get("radius",0)) * sy / z
                crect = fitz.Rect(left,top,left+rx*2,top+ry*2)
                if crect.is_empty: continue
                ann = page.add_circle_annot(crect)
                ann.set_colors(stroke=c_stroke,fill=None)
                ann.set_border(width=sw); ann.update()
                committed += 1

            # ── line / arrow ──
            elif kind == "line":
                x1 = obj.get("x1",0); y1 = obj.get("y1",0)
                x2 = obj.get("x2",0); y2 = obj.get("y2",0)
                bx = left + (obj.get("width",0)*sx/z)/2
                by = top  + (obj.get("height",0)*sy/z)/2
                ax1=bx+x1/z; ay1=by+y1/z; ax2=bx+x2/z; ay2=by+y2/z
                if abs(ax2-ax1)<1 and abs(ay2-ay1)<1: continue
                ann = page.add_line_annot(fitz.Point(ax1,ay1),fitz.Point(ax2,ay2))
                ann.set_colors(stroke=c_stroke)
                ann.set_border(width=sw)
                if obj.get("_arrow"):
                    ann.set_line_ends(fitz.PDF_ANNOT_LE_NONE,
                                      fitz.PDF_ANNOT_LE_OPEN_ARROW)
                ann.update()
                committed += 1

            # ── pen path ──
            elif kind == "path":
                ox=obj.get("left",0)/z; oy=obj.get("top",0)/z
                pts = []
                for cmd in obj.get("path",[]):
                    if not cmd: continue
                    op = cmd[0]
                    if op in ("M","L") and len(cmd)>=3:
                        pts.append(fitz.Point(ox+cmd[1]/z,oy+cmd[2]/z))
                    elif op=="Q" and len(cmd)>=5:
                        pts.append(fitz.Point(ox+cmd[3]/z,oy+cmd[4]/z))
                    elif op=="C" and len(cmd)>=7:
                        pts.append(fitz.Point(ox+cmd[5]/z,oy+cmd[6]/z))
                if len(pts)>=2:
                    ann = page.add_ink_annot([pts])
                    ann.set_colors(stroke=c_stroke)
                    ann.set_border(width=sw); ann.update()
                    committed += 1

        except Exception as e:
            st.warning(f"⚠️ commit error ({kind}): {e}")

    set_draft([])
    st.session_state.canvas_key += 1
    return committed

# ──────────────────────────────────────────────────────────────
# CSS
# ──────────────────────────────────────────────────────────────
CSS = """
<style>
header[data-testid="stHeader"]{display:none}
section[data-testid="stSidebar"]>div{padding-top:0.4rem}
div.stButton>button{width:100%;border-radius:6px;font-size:13px;padding:4px 6px}
.badge-font{background:#eaf4fb;border-left:4px solid #3498db;
            padding:4px 10px;border-radius:4px;font-size:12px;margin:4px 0}
.info-bar{background:#f0f8ff;border-left:4px solid #3498db;
          padding:6px 12px;border-radius:4px;font-size:13px;margin:4px 0}
</style>
"""

# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────
def main():
    st.set_page_config(page_title="📄 PDF Annotator",
                       layout="wide", initial_sidebar_state="expanded")
    st.markdown(CSS, unsafe_allow_html=True)
    init_state()

    if not HAS_CANVAS:
        st.error("pip install streamlit-drawable-canvas"); return

    doc = st.session_state.doc

    # ════════════════════════════════════════════
    # SIDEBAR
    # ════════════════════════════════════════════
    with st.sidebar:
        st.markdown("## 📄 PDF Annotator")

        # ── upload ──
        uploaded = st.file_uploader("อัปโหลด PDF", type=["pdf"],
                                     label_visibility="collapsed")
        if uploaded and uploaded.name != st.session_state.doc_name:
            if st.session_state.doc: st.session_state.doc.close()
            st.session_state.doc           = fitz.open("pdf", uploaded.read())
            st.session_state.doc_name      = uploaded.name
            st.session_state.current_page  = 0
            st.session_state.undo_stack.clear()
            st.session_state.redo_stack.clear()
            st.session_state.draft_objects = {}
            st.session_state.canvas_key    = 0
            doc = st.session_state.doc
            st.rerun()

        if doc:
            st.download_button("📥 บันทึกไฟล์ PDF",
                               data=doc.tobytes(),
                               file_name=st.session_state.doc_name or "annotated.pdf",
                               mime="application/pdf",
                               type="primary", use_container_width=True)

            u,r = st.columns(2)
            with u:
                if st.button("↶ Undo",
                             disabled=not st.session_state.undo_stack,
                             use_container_width=True):
                    do_undo(); st.rerun()
            with r:
                if st.button("↷ Redo",
                             disabled=not st.session_state.redo_stack,
                             use_container_width=True):
                    do_redo(); st.rerun()

            st.divider()

            # ── tools ──
            st.markdown("**🛠 เครื่องมือ**")

            def tool_btn(label, tid):
                active = st.session_state.tool == tid
                if st.button(label, key=f"tb_{tid}",
                             type="primary" if active else "secondary",
                             use_container_width=True):
                    st.session_state.tool = tid; st.rerun()

            c1,c2 = st.columns(2)
            with c1:
                tool_btn("🖱 เลือก/ลาก",  TOOL_SELECT)
                tool_btn("🖊 ปากกา",       TOOL_PEN)
                tool_btn("⬛ สี่เหลี่ยม",  TOOL_RECT)
                tool_btn("➡ ลูกศร",       TOOL_ARROW)
                tool_btn("⬛ ปิดทับ",      TOOL_REDACT)
            with c2:
                tool_btn("📝 ข้อความ",    TOOL_TEXT)
                tool_btn("🖍 ไฮไลท์",     TOOL_HIGHLIGHT)
                tool_btn("⬭ วงรี",         TOOL_CIRCLE)
                tool_btn("╱ เส้น",         TOOL_LINE)

            n_draft = len(current_draft())
            st.markdown(
                f"<div style='font-size:12px;color:#555;padding:2px 4px;'>"
                f"เครื่องมือ: <b>{st.session_state.tool}</b> &nbsp;|&nbsp; "
                f"Draft: <b>{n_draft}</b> วัตถุ</div>",
                unsafe_allow_html=True)

            # ── colour pickers + eyedropper ──
            def color_row(label, key):
                pc,bc = st.columns([3,1])
                with pc:
                    st.session_state[key] = st.color_picker(
                        label, st.session_state[key], key=f"cp_{key}")
                with bc:
                    st.markdown("<div style='margin-top:24px'>",
                                unsafe_allow_html=True)
                    active = (st.session_state.eyedrop_mode and
                              st.session_state.eyedrop_target == key)
                    if st.button("💉✓" if active else "💉",
                                 key=f"ed_{key}",
                                 type="primary" if active else "secondary",
                                 use_container_width=True):
                        if active:
                            st.session_state.eyedrop_mode = False
                        else:
                            st.session_state.eyedrop_mode   = True
                            st.session_state.eyedrop_target = key
                            st.session_state.canvas_key    += 1
                        st.rerun()
                    st.markdown("</div>", unsafe_allow_html=True)

            with st.expander("🎨 สีและความหนา", expanded=False):
                color_row("สีปากกา",         "pen_color")
                color_row("สีรูปร่าง/ลูกศร", "annot_color")
                color_row("สีกล่องปิดทับ",   "redact_color")
                if st.session_state.eyedrop_mode:
                    tgt_lbl = {"pen_color":"สีปากกา",
                               "annot_color":"สีรูปร่าง",
                               "redact_color":"สีกล่องปิดทับ",
                               "text_color":"สีข้อความ"
                               }.get(st.session_state.eyedrop_target,"?")
                    st.info(f"💉 คลิกบน PDF → **{tgt_lbl}**")
                    if st.session_state.eyedrop_result:
                        st.markdown(
                            f"<div style='background:{st.session_state.eyedrop_result};"
                            f"padding:5px 10px;border-radius:4px;color:#fff;"
                            f"text-shadow:0 0 3px #000;font-size:12px'>"
                            f"สีที่ดูด: {st.session_state.eyedrop_result}</div>",
                            unsafe_allow_html=True)
                st.session_state.line_width = st.slider(
                    "ความหนาเส้น",1,15,st.session_state.line_width)

            st.divider()

            # ── text + font ──
            st.markdown("**📝 เพิ่มข้อความ**")
            with st.expander("ตั้งค่าและวางข้อความ", expanded=False):

                pc2,bc2 = st.columns([3,1])
                with pc2:
                    st.session_state.text_color = st.color_picker(
                        "สีข้อความ",
                        st.session_state.text_color,
                        key="cp_text_color")
                with bc2:
                    st.markdown("<div style='margin-top:24px'>",
                                unsafe_allow_html=True)
                    act2 = (st.session_state.eyedrop_mode and
                            st.session_state.eyedrop_target=="text_color")
                    if st.button("💉✓" if act2 else "💉",
                                 key="ed_text_color",
                                 type="primary" if act2 else "secondary",
                                 use_container_width=True):
                        if act2:
                            st.session_state.eyedrop_mode = False
                        else:
                            st.session_state.eyedrop_mode   = True
                            st.session_state.eyedrop_target = "text_color"
                            st.session_state.canvas_key    += 1
                        st.rerun()
                    st.markdown("</div>", unsafe_allow_html=True)

                st.session_state.font_size = st.slider(
                    "ขนาดตัวอักษร (pt)", 8, 72,
                    st.session_state.font_size)

                # ── Font selector ──
                st.markdown("**🔤 เลือก Font**")
                font_src = st.radio(
                    "แหล่ง Font",
                    ["Built-in","จากเครื่อง (System)","อัปโหลดไฟล์ Font"],
                    horizontal=True, key="font_src_radio",
                    label_visibility="collapsed")

                if font_src == "Built-in":
                    st.session_state.font_source = "builtin"
                    names = list(BUILTIN_FONTS.keys())
                    cur   = st.session_state.font_name
                    idx   = names.index(cur) if cur in names else 0
                    chosen = st.selectbox("เลือก Font", names,
                                          index=idx, key="bi_sel")
                    st.session_state.font_name = chosen
                    prev = render_font_preview()
                    if prev:
                        st.markdown(
                            f"<img src='{prev}' style='width:100%;"
                            f"border:1px solid #ddd;border-radius:4px'>",
                            unsafe_allow_html=True)

                elif font_src == "จากเครื่อง (System)":
                    st.session_state.font_source = "system"
                    sys_fonts = scan_system_fonts()
                    if sys_fonts:
                        names = sorted(sys_fonts)
                        prev_sel = st.session_state.get("_sys_sel", names[0])
                        chosen = st.selectbox(
                            f"Font บนเครื่อง ({len(sys_fonts)})",
                            names,
                            index=names.index(prev_sel)
                                  if prev_sel in names else 0,
                            key="sys_sel")
                        st.session_state._sys_sel  = chosen
                        st.session_state.font_name = chosen
                        st.session_state.font_path = sys_fonts[chosen]
                        prev = render_font_preview(
                            font_path=sys_fonts[chosen])
                        if prev:
                            st.markdown(
                                f"<img src='{prev}' style='width:100%;"
                                f"border:1px solid #ddd;border-radius:4px'>",
                                unsafe_allow_html=True)
                        st.caption(f"📂 {sys_fonts[chosen]}")
                    else:
                        st.warning("ไม่พบ Font บนเครื่อง")

                else:  # upload
                    st.session_state.font_source = "upload"
                    uf = st.file_uploader(
                        "ไฟล์ Font (.ttf/.otf)",
                        type=["ttf","otf","TTF","OTF"],
                        key="font_up")
                    if uf:
                        fb = uf.read()
                        st.session_state.font_bytes = fb
                        st.session_state.font_name  = (
                            os.path.splitext(uf.name)[0])
                        prev = render_font_preview(font_bytes=fb)
                        if prev:
                            st.markdown(
                                f"<img src='{prev}' style='width:100%;"
                                f"border:1px solid #ddd;border-radius:4px'>",
                                unsafe_allow_html=True)
                        st.success(f"✅ {st.session_state.font_name}")
                    elif st.session_state.font_bytes:
                        st.info(f"Font: {st.session_state.font_name}")

                st.markdown(
                    f"<div class='badge-font'>🔤 "
                    f"<b>{st.session_state.font_name}</b> "
                    f"<span style='color:#888'>({st.session_state.font_source})"
                    f"</span></div>",
                    unsafe_allow_html=True)

                txt = st.text_area("ข้อความ", key="txt_in", height=70)
                cx_col,cy_col = st.columns(2)
                with cx_col: xp = st.slider("X (%)",0,100,10,key="tx")
                with cy_col: yp = st.slider("Y (%)",0,100,20,key="ty")

                if st.button("➕ วางข้อความบน PDF",
                             type="primary", use_container_width=True):
                    if add_text_to_draft(txt, xp, yp):
                        st.success("วางแล้ว — สลับ 🖱 เลือก/ลาก เพื่อย้ายตำแหน่ง")
                        st.rerun()
                    else:
                        st.warning("กรุณาพิมพ์ข้อความก่อน")

            st.divider()

            # ── page management ──
            st.markdown("**📄 จัดการหน้า**")
            pg1,pg2 = st.columns(2)
            with pg1:
                if st.button("🗑 ลบหน้านี้",use_container_width=True):
                    delete_current_page(); st.rerun()
                if st.button("↻ หมุน 90°", use_container_width=True):
                    rotate_current_page(90); st.rerun()
            with pg2:
                if st.button("+ หน้าว่าง", use_container_width=True):
                    insert_blank_page(); st.rerun()
                if st.button("↻ หมุน 180°",use_container_width=True):
                    rotate_current_page(180); st.rerun()

            with st.expander("📎 แทรกไฟล์ PDF อื่น", expanded=False):
                src_pdf = st.file_uploader("เลือก PDF",
                                            type=["pdf"], key="ins_pdf")
                if src_pdf and st.button("แทรกหลังหน้าปัจจุบัน",
                                          use_container_width=True):
                    save_state()
                    src = fitz.open("pdf", src_pdf.read())
                    n_ins = len(src)
                    after = st.session_state.current_page
                    for i in range(n_ins):
                        doc.insert_pdf(src,from_page=i,to_page=i,
                                       start_at=after+i+1)
                    src.close()
                    st.session_state.canvas_key += 1
                    st.success(f"แทรก {n_ins} หน้าสำเร็จ"); st.rerun()

            st.divider()

            # ── thumbnails ──
            st.markdown("**🖼 หน้าเอกสาร**")
            for i in range(total_pages()):
                is_cur = i == st.session_state.current_page
                try:
                    th = get_thumb(i)
                    if is_cur:
                        st.markdown(
                            "<div style='border:3px solid #3498DB;"
                            "border-radius:4px;margin:2px 0'>",
                            unsafe_allow_html=True)
                    st.image(th,
                             caption=f"หน้า {i+1}{'  ◀' if is_cur else ''}",
                             use_container_width=True)
                    if is_cur:
                        st.markdown("</div>", unsafe_allow_html=True)
                except Exception:
                    pass
                if st.button(
                        f"{'▶ ' if is_cur else ''}หน้า {i+1}",
                        key=f"pg_{i}", use_container_width=True,
                        type="primary" if is_cur else "secondary"):
                    go_page(i); st.rerun()

    # ════════════════════════════════════════════
    # MAIN CONTENT
    # ════════════════════════════════════════════
    if not doc:
        st.info("👈 กรุณาอัปโหลดไฟล์ PDF"); return

    # ── nav bar ──
    n1,n2,n3,n4,n5,n6,n7 = st.columns([1.2,1.2,2,1.2,1.2,1.5,2])
    with n1:
        if st.button("◀ ก่อนหน้า", use_container_width=True):
            if st.session_state.current_page > 0:
                st.session_state.current_page -= 1
                st.session_state.canvas_key  += 1; st.rerun()
    with n2:
        if st.button("ถัดไป ▶", use_container_width=True):
            if st.session_state.current_page < total_pages()-1:
                st.session_state.current_page += 1
                st.session_state.canvas_key  += 1; st.rerun()
    with n3:
        st.markdown(
            f"<div style='text-align:center;padding-top:6px;"
            f"font-weight:bold;font-size:15px;'>หน้า "
            f"{st.session_state.current_page+1} / {total_pages()}</div>",
            unsafe_allow_html=True)
    with n4:
        if st.button("🔍+", use_container_width=True):
            st.session_state.zoom = min(
                st.session_state.zoom+ZOOM_STEP,ZOOM_MAX)
            st.rerun()
    with n5:
        if st.button("🔍-", use_container_width=True):
            st.session_state.zoom = max(
                st.session_state.zoom-ZOOM_STEP,ZOOM_MIN)
            st.rerun()
    with n6:
        st.markdown(
            f"<div style='text-align:center;padding-top:6px;font-size:14px;'>"
            f"ซูม {int(st.session_state.zoom*100)}%</div>",
            unsafe_allow_html=True)
    with n7:
        if st.button("🗑 ลบ Annotation ทั้งหน้า", use_container_width=True):
            delete_all_annots(); st.rerun()

    st.markdown("---")

    # ── info bar: tool hint ──
    tool = st.session_state.tool
    hints = {
        TOOL_SELECT:    "🖱 เลือก/ลาก — คลิกวัตถุเพื่อเลือก แล้วลากเพื่อย้าย / ลาก handle เพื่อ resize",
        TOOL_PEN:       "🖊 ปากกา — ลากวาดเส้นอิสระ",
        TOOL_RECT:      "⬛ สี่เหลี่ยม — คลิกลากเพื่อวาดกล่อง",
        TOOL_CIRCLE:    "⬭ วงรี — คลิกลากเพื่อวาดวงรี",
        TOOL_LINE:      "╱ เส้น — คลิกจุดเริ่มแล้วลากไปจุดสิ้นสุด",
        TOOL_ARROW:     "➡ ลูกศร — คลิกลากทิศทางลูกศร",
        TOOL_HIGHLIGHT: "🖍 ไฮไลท์ — ลากคลุมข้อความ",
        TOOL_REDACT:    "⬛ ปิดทับ — ลากสร้างกล่องทึบบนข้อความ",
        TOOL_TEXT:      "📝 ข้อความ — วางข้อความผ่าน Sidebar แล้วสลับ 🖱 เลือก/ลาก เพื่อย้าย",
    }
    st.markdown(
        f"<div class='info-bar'>{hints.get(tool,'')}</div>",
        unsafe_allow_html=True)

    bg = get_page_image()
    if bg is None:
        st.error("โหลดหน้า PDF ไม่ได้"); return

    # ── eyedrop mode ──
    eyedrop_active = st.session_state.eyedrop_mode
    if eyedrop_active:
        t_label = {"pen_color":"สีปากกา","annot_color":"สีรูปร่าง/ลูกศร",
                   "redact_color":"สีกล่องปิดทับ","text_color":"สีข้อความ"
                   }.get(st.session_state.eyedrop_target,"?")
        st.info(f"💉 คลิกบน PDF เพื่อดูดสีไปยัง **{t_label}**")
        col_cancel,_ = st.columns([1,4])
        with col_cancel:
            if st.button("✖ ยกเลิก", use_container_width=True):
                st.session_state.eyedrop_mode = False
                st.session_state.canvas_key  += 1; st.rerun()

    # ── canvas mode ──
    if eyedrop_active:
        c_mode="point"; c_stroke="#ff6400"; c_fill="rgba(255,100,0,0.4)"
        sw = 6
    elif tool == TOOL_SELECT or tool == TOOL_TEXT:
        c_mode="transform"; c_stroke=st.session_state.annot_color
        c_fill="rgba(0,0,0,0)"; sw=st.session_state.line_width
    elif tool == TOOL_PEN:
        c_mode="freedraw"; c_stroke=st.session_state.pen_color
        c_fill="rgba(0,0,0,0)"; sw=st.session_state.line_width
    elif tool == TOOL_RECT:
        c_mode="rect"; c_stroke=st.session_state.annot_color
        c_fill="rgba(0,0,0,0)"; sw=st.session_state.line_width
    elif tool == TOOL_CIRCLE:
        c_mode="circle"; c_stroke=st.session_state.annot_color
        c_fill="rgba(0,0,0,0)"; sw=st.session_state.line_width
    elif tool in (TOOL_LINE,TOOL_ARROW):
        c_mode="line"; c_stroke=st.session_state.annot_color
        c_fill="rgba(0,0,0,0)"; sw=st.session_state.line_width
    elif tool == TOOL_HIGHLIGHT:
        c_mode="rect"; c_stroke=HL_STROKE; c_fill=HL_FILL
        sw=st.session_state.line_width
    elif tool == TOOL_REDACT:
        c_mode="rect"
        c_stroke=st.session_state.redact_color
        c_fill=st.session_state.redact_color
        sw=st.session_state.line_width
    else:
        c_mode="freedraw"; c_stroke="#000"; c_fill="rgba(0,0,0,0)"
        sw=st.session_state.line_width

    init_data = draft_as_initial_json()

    # ── render canvas ──
    if eyedrop_active:
        canvas_res = st_canvas(
            fill_color=c_fill, stroke_width=6, stroke_color=c_stroke,
            background_image=bg, update_streamlit=True,
            height=bg.height, width=bg.width,
            drawing_mode="point", point_display_radius=5,
            display_toolbar=False,
            key=f"cv_{st.session_state.current_page}_{st.session_state.canvas_key}"
        )
        if (canvas_res and canvas_res.json_data and
                canvas_res.json_data.get("objects")):
            pt  = canvas_res.json_data["objects"][-1]
            cx_pt = int(pt.get("left",0))
            cy_pt = int(pt.get("top",0))
            sampled = sample_color_from_page(bg,cx_pt,cy_pt)
            tgt = st.session_state.eyedrop_target
            st.session_state[tgt]           = sampled
            st.session_state.eyedrop_result = sampled
            st.session_state.eyedrop_mode   = False
            st.session_state.canvas_key    += 1
            st.success(f"💉 ดูดสี {sampled} → {t_label}")
            st.rerun()
    else:
        canvas_res = st_canvas(
            fill_color=c_fill,
            stroke_width=sw,
            stroke_color=c_stroke,
            background_image=bg,
            initial_drawing=init_data,
            update_streamlit=True,   # รับ JSON ทุก interaction (drag/resize)
            height=bg.height, width=bg.width,
            drawing_mode=c_mode,
            display_toolbar=True,
            key=f"cv_{st.session_state.current_page}_{st.session_state.canvas_key}"
        )

        # ── sync draft จาก canvas ──
        if canvas_res and canvas_res.json_data:
            # เพิ่ม metadata _arrow ให้ object line ที่วาดใหม่ใน arrow mode
            objs = canvas_res.json_data.get("objects") or []
            if tool == TOOL_ARROW:
                for o in objs:
                    if o.get("type") == "line" and "_arrow" not in o:
                        o["_arrow"] = True
            elif tool == TOOL_HIGHLIGHT:
                for o in objs:
                    if o.get("type") == "rect" and "_kind" not in o:
                        o["_kind"] = "highlight"
            elif tool == TOOL_REDACT:
                for o in objs:
                    if o.get("type") == "rect" and "_kind" not in o:
                        o["_kind"] = "redact"
            canvas_res.json_data["objects"] = objs
            sync_draft_from_canvas(canvas_res.json_data)

    # ── action bar ──
    if not eyedrop_active:
        n_draft = len(current_draft())
        a1,a2,a3 = st.columns([3,2,2])
        with a1:
            lbl = (f"✅ Commit {n_draft} วัตถุลง PDF"
                   if n_draft else "✅ Commit Annotations")
            if st.button(lbl, type="primary",
                         use_container_width=True,
                         disabled=(n_draft==0)):
                committed = commit_draft_to_pdf()
                if committed:
                    st.success(f"✅ Commit {committed} วัตถุสำเร็จ!")
                    st.rerun()
                else:
                    st.warning("ไม่มีวัตถุใน draft")
        with a2:
            if st.button("🗑 ล้าง Draft",
                         use_container_width=True,
                         disabled=(n_draft==0)):
                set_draft([])
                st.session_state.canvas_key += 1; st.rerun()
        with a3:
            st.markdown(
                f"<div style='padding-top:8px;font-size:13px;color:#555;'>"
                f"📋 Draft: <b>{n_draft}</b> | "
                f"Undo: <b>{len(st.session_state.undo_stack)}</b></div>",
                unsafe_allow_html=True)

    # ── help ──
    with st.expander("ℹ️ วิธีใช้งาน", expanded=False):
        st.markdown("""
**การวาดและย้ายวัตถุ:**
1. เลือกเครื่องมือ → วาดบน canvas → วัตถุจะถูกเก็บใน **Draft** (ยังไม่ฝัง PDF)
2. สลับไป **🖱 เลือก/ลาก** → คลิกวัตถุ → **ลาก** ย้าย หรือลาก handle เพื่อ **resize**
3. เมื่อพอใจแล้ว กด **✅ Commit** เพื่อฝังวัตถุทั้งหมดลง PDF จริง

**📝 ข้อความ (Font เลือกได้):**
- เปิด "เพิ่มข้อความ" → เลือก Font → พิมพ์ข้อความ → กด **➕ วาง**
- Font: Built-in / จากเครื่อง (scan อัตโนมัติ) / อัปโหลดไฟล์ .ttf/.otf
- สลับ **🖱 เลือก/ลาก** แล้วลากกล่องข้อความไปตำแหน่งที่ต้องการ

**💉 Eyedropper:** กดปุ่ม 💉 ข้างช่องสี → คลิกบน PDF

**⚠️ วัตถุใน Draft ยังไม่ฝัง PDF** — ต้องกด **✅ Commit** ก่อนดาวน์โหลด
        """)


if __name__ == "__main__":
    main()
