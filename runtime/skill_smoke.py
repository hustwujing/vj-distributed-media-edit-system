#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from zipfile import ZipFile

from PIL import Image, ImageFont
from openpyxl import Workbook
from reportlab.pdfgen import canvas

from webapp.runtime_skills import materialize_skills


ROOT = Path("/opt/video-use/skills")
WORK = Path("/workspace/skill-smoke")
REQUIRED_FILES = {
    "academy-guide": ["LICENSE.txt"],
    "algorithmic-art": ["templates/generator_template.js", "templates/viewer.html"],
    "anatomy": ["kit/audit.mjs", "examples/dial-indicator/build.mjs"],
    "brand-guidelines": ["LICENSE.txt"],
    "canvas-design": ["canvas-fonts/InstrumentSans-Regular.ttf"],
    "claude-api": ["shared/models.md", "curl/examples.md"],
    "codex-ppt": ["scripts/assemble_ppt.py", "docs/workflow-gates-and-progress.md"],
    "diagram-design": ["assets/example-flowchart.html", "scripts/self_check.py"],
    "discernment-nudge": ["LICENSE.txt"],
    "doc-and-modernize": ["references/copilot-instructions.template.md", "references/migration-hazards.md"],
    "doc-coauthoring": [],
    "docx": ["scripts/office/validate.py", "scripts/merge_runs.py"],
    "drawio-skill": ["scripts/diagramctl.py", "data/diagram-ir.schema.json"],
    "frontend-design": ["LICENSE.txt"],
    "image-to-editable-ppt": ["cli/pyproject.toml", "prompts/page-worker.md"],
    "internal-comms": ["examples/3p-updates.md", "examples/faq-answers.md"],
    "mcp-builder": ["scripts/connections.py", "reference/python_mcp_server.md"],
    "pdf": ["forms.md", "scripts/convert_pdf_to_images.py"],
    "pptx": ["scripts/thumbnail.py", "scripts/office/validate.py"],
    "skill-creator": ["scripts/quick_validate.py", "references/schemas.md"],
    "slack-gif-creator": ["core/gif_builder.py", "core/validators.py"],
    "theme-factory": ["theme-showcase.pdf", "themes/modern-minimalist.md"],
    "web-artifacts-builder": ["scripts/init-artifact.sh", "scripts/shadcn-components.tar.gz"],
    "webapp-testing": ["scripts/with_server.py", "examples/static_html_automation.py"],
    "xlsx": ["scripts/recalc.py", "scripts/office/validate.py"],
}


def run(*command: str, cwd: Path | None = None) -> str:
    try:
        value = subprocess.run(command, cwd=cwd, check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"command failed: {command}\n{exc.stdout}") from exc
    return value.stdout


def main() -> None:
    if os.getuid() == 0:
        raise SystemExit("skill smoke must run as Runtime UID 10001")
    shutil.rmtree(WORK, ignore_errors=True);WORK.mkdir(parents=True)
    results = {"harnesses": {}, "artifacts": {}, "skills": {}}
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert {row["id"] for row in manifest["skills"]} == set(REQUIRED_FILES)
    for row in manifest["skills"]:
        skill_id = row["id"]
        skill_root = ROOT / skill_id
        entry = (skill_root / "SKILL.md").read_text()
        name = re.search(r"^name:\s*['\"]?([a-z0-9][a-z0-9-]*)", entry, re.MULTILINE)
        assert name and name.group(1) == skill_id and len(entry) >= 200
        for relative in REQUIRED_FILES[skill_id]:
            path = skill_root / relative
            assert path.is_file() and path.stat().st_size > 0, f"{skill_id}: {relative}"
        results["skills"][skill_id] = {
            "passed": True,
            "evidence": ["entry_contract", *REQUIRED_FILES[skill_id]],
        }

    def evidence(skill_id: str, *values: str) -> None:
        results["skills"][skill_id]["evidence"].extend(values)

    for harness in ("codex", "claude_code", "deepseek_harness"):
        installed = materialize_skills(harness,source=ROOT)
        root = Path(installed["root"])
        assert len(list(root.glob("*/SKILL.md"))) == 25
        results["harnesses"][harness] = installed["skill_count"]

    run("node", "--check", str(ROOT / "algorithmic-art/templates/generator_template.js"))
    assert "canvas" in (ROOT / "algorithmic-art/templates/viewer.html").read_text().lower()
    evidence("algorithmic-art", "node_syntax", "viewer_canvas")

    fonts = sorted((ROOT / "canvas-design/canvas-fonts").glob("*.ttf"))
    assert len(fonts) >= 20
    for font in fonts:
        ImageFont.truetype(str(font), 18)
    evidence("canvas-design", f"fonts_loaded:{len(fonts)}")

    run("python3", "-c", "import anthropic; import mcp")
    evidence("claude-api", "anthropic_import")
    run("python3", "-c", "import connections", cwd=ROOT / "mcp-builder/scripts")
    evidence("mcp-builder", "mcp_connection_import")

    graph = WORK / "graph.json"
    graph.write_text(json.dumps({"direction":"LR","nodes":[{"id":"api","label":"API"},
        {"id":"worker","label":"Worker"}],"edges":[{"source":"api","target":"worker","label":"job"}]}))
    drawio = WORK / "architecture.drawio";drawio_png = WORK / "architecture.png"
    run("python3",str(ROOT/"drawio-skill/scripts/autolayout.py"),str(graph),"-o",str(drawio))
    run("python3",str(ROOT/"drawio-skill/scripts/validate.py"),str(drawio),"--score")
    run("drawio","-x","-f","png","--width","800","-o",str(drawio_png),str(drawio))
    with Image.open(drawio_png) as image:assert image.width>100 and image.height>50
    results["artifacts"]["drawio_png"] = drawio_png.stat().st_size
    evidence("drawio-skill", "validated_drawio", "exported_png")

    diagram_html = ROOT / "diagram-design/assets/example-flowchart.html"
    diagram_png = WORK / "diagram-design.png"
    run("python3",str(ROOT/"diagram-design/scripts/self_check.py"),str(diagram_html))
    run("python3",str(ROOT/"diagram-design/scripts/export_png.py"),str(diagram_html),str(diagram_png),"1")
    with Image.open(diagram_png) as image:assert image.width>=900 and image.height>=600
    results["artifacts"]["diagram_design_png"] = diagram_png.stat().st_size
    evidence("diagram-design", "self_check", "exported_png")

    anatomy = WORK / "anatomy";shutil.copytree(ROOT/"anatomy",anatomy)
    run("chmod", "-R", "u+w", str(anatomy))
    run("node","examples/dial-indicator/build.mjs",cwd=anatomy)
    anatomy_html = anatomy / "examples/dial-indicator/dial-indicator.html"
    anatomy_png = WORK / "anatomy.png"
    run("node",str(anatomy/"scripts/capture.mjs"),str(anatomy_html),"--out",str(anatomy_png),"--width","390",cwd=anatomy)
    with Image.open(anatomy_png) as image:assert image.width>=390 and image.height>300
    results["artifacts"]["anatomy_png"] = anatomy_png.stat().st_size
    run("node", "--test", "scripts/lines.test.mjs", cwd=anatomy)
    evidence("anatomy", "example_build", "browser_capture", "node_test")

    docx = WORK / "document.docx"
    node_docx = WORK / "document.cjs"
    node_docx.write_text("""const fs=require('fs');const {Document,Packer,Paragraph}=require('docx');
const d=new Document({sections:[{children:[new Paragraph('updream-harness skill smoke')]}]});
Packer.toBuffer(d).then(b=>fs.writeFileSync(process.argv[2],b));\n""")
    run("node",str(node_docx),str(docx))
    run("python3",str(ROOT/"docx/scripts/office/soffice.py"),"--headless","--convert-to","pdf","--outdir",str(WORK),str(docx))
    assert (WORK/"document.pdf").is_file()
    results["artifacts"]["docx"] = docx.stat().st_size
    evidence("docx", "created_docx", "libreoffice_pdf")

    deck_dir=WORK/"deck"/"SmokeDeck"/"origin_image";deck_dir.mkdir(parents=True)
    Image.new("RGB",(1280,720),(35,91,78)).save(deck_dir/"slide_01.png")
    run("python3",str(ROOT/"codex-ppt/scripts/assemble_ppt.py"),str(WORK/"deck"),"SmokeDeck.pptx")
    pptx=WORK/"deck"/"SmokeDeck"/"SmokeDeck.pptx"
    with ZipFile(pptx) as archive:assert "ppt/slides/slide1.xml" in archive.namelist()
    run("python3",str(ROOT/"pptx/scripts/office/soffice.py"),"--headless","--convert-to","pdf","--outdir",str(WORK),str(pptx))
    assert (WORK/"SmokeDeck.pdf").is_file()
    results["artifacts"]["pptx"] = pptx.stat().st_size
    evidence("codex-ppt", "assembled_pptx")
    evidence("pptx", "validated_pptx", "libreoffice_pdf")

    workbook=Workbook();sheet=workbook.active;sheet["A1"]=2;sheet["A2"]=3;sheet["A3"]="=SUM(A1:A2)"
    xlsx=WORK/"formula.xlsx";workbook.save(xlsx)
    recalc=json.loads(run("python3",str(ROOT/"xlsx/scripts/recalc.py"),str(xlsx)))
    assert recalc["status"]=="success" and recalc["total_errors"]==0
    results["artifacts"]["xlsx"] = xlsx.stat().st_size
    evidence("xlsx", "formula_recalculation")

    pdf=WORK/"report.pdf";writer=canvas.Canvas(str(pdf));writer.drawString(72,720,"updream-harness skill smoke");writer.save()
    run("qpdf","--check",str(pdf));run("pdftoppm","-png","-singlefile",str(pdf),str(WORK/"report"))
    assert (WORK/"report.png").is_file()
    results["artifacts"]["pdf"] = pdf.stat().st_size
    evidence("pdf", "qpdf_check", "poppler_render")

    source=WORK/"source.png";Image.new("RGB",(640,360),(245,245,240)).save(source)
    doctor=json.loads(run("editppt","doctor","--json"))
    assert all(doctor["dependencies"].values())
    edit_run=WORK/"editppt-run"
    run("editppt","prepare",str(source),"--job-dir",str(edit_run),"--no-text-hints")
    assert (edit_run/"deck_manifest.json").is_file() and (edit_run/"pages/page_001/source.png").is_file()
    results["artifacts"]["editppt_manifest"] = (edit_run/"deck_manifest.json").stat().st_size
    evidence("image-to-editable-ppt", "doctor", "prepared_manifest")

    gif_script = WORK / "gif-smoke.py"
    gif_script.write_text("""import sys
sys.path.insert(0, sys.argv[2])
from core.frame_composer import create_blank_frame, draw_circle
from core.gif_builder import GIFBuilder
b=GIFBuilder(64,64,5)
for x in (16,32,48):
 f=create_blank_frame(64,64,(255,255,255));draw_circle(f,(x,32),8,(34,120,210));b.add_frame(f)
print(b.save(sys.argv[1]))
""")
    gif_dir = ROOT / "slack-gif-creator"
    gif = WORK / "skill.gif"
    run("python3", str(gif_script), str(gif), str(gif_dir), cwd=gif_dir)
    assert gif.is_file() and gif.stat().st_size > 0
    evidence("slack-gif-creator", "animated_gif")

    themes = sorted((ROOT / "theme-factory/themes").glob("*.md"))
    assert len(themes) == 10 and all(path.stat().st_size > 400 for path in themes)
    evidence("theme-factory", "ten_theme_contracts")

    sample_skill = WORK / "sample-skill";sample_skill.mkdir()
    (sample_skill / "SKILL.md").write_text("""---
name: sample-skill
description: Use this skill to validate a complete sample skill in the Runtime image.
---
# Sample skill
Follow the requested sample workflow and return the resulting artifact.
""")
    run("python3", str(ROOT / "skill-creator/scripts/quick_validate.py"), str(sample_skill))
    evidence("skill-creator", "quick_validate")

    run("bash", "-n", str(ROOT / "web-artifacts-builder/scripts/init-artifact.sh"))
    run("bash", "-n", str(ROOT / "web-artifacts-builder/scripts/bundle-artifact.sh"))
    run("tar", "-tzf", str(ROOT / "web-artifacts-builder/scripts/shadcn-components.tar.gz"))
    evidence("web-artifacts-builder", "shell_syntax", "component_archive")

    run("python3", str(ROOT / "webapp-testing/scripts/with_server.py"), "--help")
    evidence("webapp-testing", "server_runner_cli")

    for skill_id in ("academy-guide", "brand-guidelines", "discernment-nudge",
            "doc-and-modernize", "doc-coauthoring", "frontend-design", "internal-comms"):
        evidence(skill_id, "instruction_resource_contract")

    assert len(results["skills"]) == 25

    print(json.dumps({"passed":True,**results},sort_keys=True))


if __name__ == "__main__":
    main()
