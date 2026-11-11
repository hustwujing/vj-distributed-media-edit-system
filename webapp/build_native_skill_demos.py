"""Build the 25 Native Skill examples plus one real multi-media generation example."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import textwrap

from PIL import Image,ImageDraw,ImageFont


REPO=Path(__file__).resolve().parents[1]
ROOT=Path(os.environ.get('VIDEO_USE_DEMO_SOURCE_ROOT',REPO/'workspace/demos')).resolve()
MANIFEST=REPO/'runtime/skills/manifest.json'
PREFIX='skill-'
MEDIA_ID='generate-image-video-audio'


SPECS={
 'academy-guide':('编写一份新成员学习指南','知识与培训','把零散学习目标组织成分阶段、可执行的学院式指南。',
   '使用已安装的 `academy-guide` Native Skill，由 OpenSandbox Harness 编写一份面向新入职后端工程师的四周学习指南，包含目标、每日练习、检查点和毕业标准。只交付 academy-guide.md。',['academy-guide.md']),
 'algorithmic-art':('生成可复现的算法艺术','创意编程','使用确定性种子生成可在浏览器检查的互动艺术。',
   '使用已安装的 `algorithmic-art` Native Skill，由 OpenSandbox Harness 创作一幅以“城市雨夜数据流”为主题的可复现算法艺术。交付 algorithmic-art.html 和 algorithmic-art.png，HTML 可离线打开，固定随机种子 42。',['algorithmic-art.html','algorithmic-art.png']),
 'anatomy':('制作交互式结构剖解图','交互式图解','用分层结构、标注和交互讲清复杂对象。',
   '使用已安装的 `anatomy` Native Skill，由 OpenSandbox Harness 制作一份“机械键盘工作原理”交互式结构剖解图。交付 keyboard-anatomy.html 和 keyboard-anatomy.png，离线页面须能切换部件说明。',['keyboard-anatomy.html','keyboard-anatomy.png']),
 'brand-guidelines':('整理一份品牌使用规范','品牌规范','将品牌定位落实为颜色、字体、语气和使用禁区。',
   '使用已安装的 `brand-guidelines` Native Skill，由 OpenSandbox Harness 为虚构产品 Northstar Notes 编写品牌规范，包含定位、颜色、字体、语气、Logo 留白和错误示例。只交付 brand-guidelines.md。',['brand-guidelines.md']),
 'canvas-design':('设计一张活动主视觉','平面设计','输出有明确视觉层级和版式逻辑的静态画布。',
   '使用已安装的 `canvas-design` Native Skill，由 OpenSandbox Harness 设计一张 1600×900 的“Build Night 2026”技术活动主视觉，信息层级清楚、无渐变、适合大屏展示。只交付 build-night.png。',['build-night.png']),
 'claude-api':('编写 Claude API 接入说明','API 集成','提供可执行的 SDK 示例、错误处理和安全边界。',
   '使用已安装的 `claude-api` Native Skill，由 OpenSandbox Harness 编写 Python 接入示例和说明，演示流式文本、工具调用、超时与错误处理；不得写入真实密钥。交付 claude-api-guide.md 和 claude_example.py。',['claude-api-guide.md','claude_example.py']),
 'codex-ppt':('生成项目启动演示稿','演示文稿','按清晰叙事结构生成可编辑的项目汇报幻灯片。',
   '使用已安装的 `codex-ppt` Native Skill，由 OpenSandbox Harness 制作一份 6 页“智能素材工作流项目启动会”演示稿，包含目标、架构、里程碑、风险和下一步。只交付 project-kickoff.pptx。',['project-kickoff.pptx']),
 'diagram-design':('绘制服务架构图','架构图','输出可读、可缩放并经过浏览器检查的系统图。',
   '使用已安装的 `diagram-design` Native Skill，由 OpenSandbox Harness 绘制一个 API、调度器、OpenSandbox、对象存储和数据库组成的服务架构图。交付 architecture.html 和 architecture.png。',['architecture.html','architecture.png']),
 'discernment-nudge':('审查方案中的薄弱假设','决策审查','区分事实、假设和风险，形成可行动的审查意见。',
   '使用已安装的 `discernment-nudge` Native Skill，由 OpenSandbox Harness 审查“所有任务都迁移到 GPU 节点”的提案，列出事实、未经验证的假设、反例、决策门槛和建议实验。只交付 decision-review.md。',['decision-review.md']),
 'doc-and-modernize':('现代化一份旧项目说明','代码文档','把遗留项目整理成可维护的结构、说明和迁移计划。',
   '使用已安装的 `doc-and-modernize` Native Skill，由 OpenSandbox Harness 为一个虚构的 Python 2 批处理脚本项目编写现代化文档，明确当前问题、Python 3 迁移步骤、测试策略和回滚方案。交付 README.md 和 migration-plan.md。',['README.md','migration-plan.md']),
 'doc-coauthoring':('协作撰写技术提案','协作文档','通过受众、目标和评审问题组织一份可讨论的提案。',
   '使用已安装的 `doc-coauthoring` Native Skill，由 OpenSandbox Harness 起草“统一沙箱执行接口”技术提案，包含背景、非目标、方案、备选项、风险和待确认问题。只交付 sandbox-proposal.md。',['sandbox-proposal.md']),
 'docx':('生成正式 Word 项目简报','Word 文档','创建带标题、表格、页眉页脚和一致样式的 DOCX。',
   '使用已安装的 `docx` Native Skill，由 OpenSandbox Harness 创建一份两页项目周报，包含摘要、进展表格、风险列表和下一周计划。只交付 weekly-brief.docx。',['weekly-brief.docx']),
 'drawio-skill':('生成可编辑 Draw.io 图','流程图','同时交付可编辑源文件与可审查的导出图。',
   '使用已安装的 `drawio-skill` Native Skill，由 OpenSandbox Harness 绘制“用户请求到沙箱交付”的流程图，包含确认、调度、执行、验收和清理。交付 workflow.drawio 和 workflow.png。',['workflow.drawio','workflow.png']),
 'frontend-design':('制作安静高效的运营面板','前端设计','实现适合重复操作和信息扫描的响应式工作界面。',
   '使用已安装的 `frontend-design` Native Skill，由 OpenSandbox Harness 制作一个响应式任务运营面板，包含筛选、任务表、状态、耗时和详情抽屉。只交付 operations-dashboard.html，离线打开即可使用。',['operations-dashboard.html']),
 'image-to-editable-ppt':('把参考图变成可编辑幻灯片','可编辑演示稿','从参考图重建可编辑的文本、形状和版式。',
   '使用已安装的 `image-to-editable-ppt` Native Skill，由 OpenSandbox Harness 把提供的参考图片重建成一页可编辑幻灯片，不得只把整张图贴进页面。交付 editable-slide.pptx 和 slide-map.json。',['editable-slide.pptx','slide-map.json']),
 'internal-comms':('编写内部变更公告','内部沟通','用清楚、可执行的结构说明影响、时间和支持渠道。',
   '使用已安装的 `internal-comms` Native Skill，由 OpenSandbox Harness 编写一次沙箱运行时升级公告，包含变更内容、影响范围、时间线、用户动作、FAQ 和支持渠道。只交付 runtime-announcement.md。',['runtime-announcement.md']),
 'mcp-builder':('搭建一个只读 MCP 服务','MCP 开发','生成结构清晰、参数受限且带测试的 MCP 服务样例。',
   '使用已安装的 `mcp-builder` Native Skill，由 OpenSandbox Harness 创建一个只读的项目状态 MCP Server，提供 list_projects 和 get_project 两个工具，使用内存样例数据。交付 server.py、README.md 和 test_server.py。',['server.py','README.md','test_server.py']),
 'pdf':('生成带目录的 PDF 报告','PDF 文档','创建可检索、可渲染并通过结构检查的 PDF。',
   '使用已安装的 `pdf` Native Skill，由 OpenSandbox Harness 生成一份 4 页“沙箱容量评估”PDF，包含摘要、容量表、风险和建议，文字必须可检索。只交付 capacity-report.pdf。',['capacity-report.pdf']),
 'pptx':('制作季度复盘演示稿','演示文稿','创建可编辑、版式一致并经过渲染检查的 PPTX。',
   '使用已安装的 `pptx` Native Skill，由 OpenSandbox Harness 制作一份 7 页季度复盘，包含指标、成果、问题、经验和下一季度计划。只交付 quarterly-review.pptx。',['quarterly-review.pptx']),
 'skill-creator':('创建一个新的检查型 Skill','Skill 开发','形成边界清楚、可验证且能复用的 Skill 包。',
   '使用已安装的 `skill-creator` Native Skill，由 OpenSandbox Harness 创建一个名为 log-summary 的 Skill，用于读取脱敏日志并输出结构化摘要，明确触发条件、步骤、安全限制和验证。只交付 SKILL.md。',['SKILL.md']),
 'slack-gif-creator':('制作轻量状态动图','GIF 动画','生成体积受控、节奏清楚且适合聊天工具的 GIF。',
   '使用已安装的 `slack-gif-creator` Native Skill，由 OpenSandbox Harness 制作一个“排队→执行→完成”的 3 秒循环状态动图，尺寸 480×270，文件不超过 2MB。只交付 workflow-status.gif。',['workflow-status.gif']),
 'theme-factory':('定义一套完整视觉主题','视觉主题','输出可以直接用于文档和页面的颜色与字体规则。',
   '使用已安装的 `theme-factory` Native Skill，由 OpenSandbox Harness 创建一套“Signal Garden”主题，包含颜色、字体、间距、图表色和无障碍说明，并制作展示页。交付 signal-garden.md 和 theme-showcase.html。',['signal-garden.md','theme-showcase.html']),
 'web-artifacts-builder':('构建一个交互式估算器','Web 产物','构建无后端依赖、可离线使用的完整交互页面。',
   '使用已安装的 `web-artifacts-builder` Native Skill，由 OpenSandbox Harness 构建一个沙箱容量估算器，可输入 CPU、内存、单任务需求并实时计算并发数。只交付 capacity-calculator.html。',['capacity-calculator.html']),
 'webapp-testing':('测试一个本地 Web 页面','Web 测试','使用浏览器自动化检查交互、响应式布局和控制台错误。',
   '使用已安装的 `webapp-testing` Native Skill，由 OpenSandbox Harness 先创建一个带表单和结果区的小页面，再启动本地服务用 Playwright 测试提交、窄屏布局和控制台错误。交付 test-report.md 和 tested-page.png。',['test-report.md','tested-page.png']),
 'xlsx':('生成带公式的预算表','Excel 表格','创建带公式、格式和校验结果的可编辑工作簿。',
   '使用已安装的 `xlsx` Native Skill，由 OpenSandbox Harness 创建一份 12 个月沙箱成本预算表，包含 CPU、内存、存储、模型费用和月度/年度合计公式。只交付 sandbox-budget.xlsx。',['sandbox-budget.xlsx']),
}


def font(size,bold=False):
    names=['/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc' if bold else '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
           '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']
    return ImageFont.truetype(next(name for name in names if Path(name).is_file()),size)


def artwork(identifier,title,outputs,target):
    digest=hashlib.sha256(identifier.encode()).digest();base=(40+digest[0]%90,45+digest[1]%90,55+digest[2]%90)
    accent=(90+digest[3]%150,90+digest[4]%150,90+digest[5]%150)
    image=Image.new('RGB',(960,540),(20,23,28));draw=ImageDraw.Draw(image)
    draw.rectangle((0,0,960,18),fill=accent);draw.rounded_rectangle((48,52,912,488),radius=8,fill=base,outline=(230,235,240),width=2)
    draw.rectangle((80,92,160,172),fill=accent);draw.text((102,105),str(len(outputs)),font=font(40,True),fill=(15,18,22))
    draw.text((190,88),'NATIVE SKILL',font=font(24,True),fill=(245,247,250));draw.text((190,126),identifier,font=font(32,True),fill=(245,247,250))
    y=220
    for line in textwrap.wrap(title,width=24)[:2]:draw.text((80,y),line,font=font(34,True),fill=(255,255,255));y+=46
    y=348
    for output in outputs[:3]:draw.ellipse((82,y+8,94,y+20),fill=accent);draw.text((112,y),output,font=font(23),fill=(238,242,246));y+=38
    image.save(target/'preview.png')
    image.resize((640,360),Image.Resampling.LANCZOS).save(target/'poster.jpg',quality=90,optimize=True)


def demo(skill,metadata,spec):
    title,category,description,prompt,outputs=spec;identifier=PREFIX+skill;target=ROOT/identifier
    target.mkdir(parents=True,exist_ok=True);artwork(skill,title,outputs,target)
    sources=[]
    if skill=='image-to-editable-ppt':sources=[{'key':'reference','name':'参考页面.png','file':'image-cup.png'}]
    return {'id':identifier,'title':title,'category':category,'description':description,
            'tags':['Native Skill',skill],'workflow':'agent','native_skill':skill,
            'prompt':prompt,'options':['运行环境：OpenSandbox Harness','必须使用 '+skill+' Skill','交付：'+ '、'.join(outputs)],
            'sources':sources,'preset':{'kind':'agent','assets':[item['key'] for item in sources]},
            'expected_outputs':outputs,'result':'preview.png','result_type':'image/png','poster':'poster.jpg',
            'files':['preview.png','poster.jpg'],'credits':[{'name':skill+' upstream',
                'url':metadata['source_url'],'license':metadata['license'],'license_url':metadata['source_url']}],
            'changes':'封面是本地生成的能力预览；载入后由所选 Native Harness 在 OpenSandbox 中真实执行并交付文件。'}


def media_demo():
    target=ROOT/MEDIA_ID;target.mkdir(parents=True,exist_ok=True)
    outputs=['image.png','video.mp4','narration.mp3']
    artwork('IMAGE + VIDEO + AUDIO','一次生成三种媒体',outputs,target)
    return {'id':MEDIA_ID,'title':'一次生成图片、视频和配音','category':'多媒体生成','description':'同一确认方案内并行生成图片、视频和独立语音，分别验收并一起交付。',
            'tags':['真实 Provider','三媒体联合生成'],'workflow':'agent','media_generation':True,
            'prompt':'先给出一个明确方案，等我确认后并行完成三项真实生成：1）用 image.generate_from_text 生成 1024×768 的蓝色纸艺灯塔夜景 PNG；2）用 video.generate_from_text 生成 5 秒、1280×720 的同主题轻微推镜 MP4；3）用 audio.generate_from_text 生成普通话男声 MP3，内容为“灯塔已经点亮，旅程现在开始。”，语速 1.0。分别验收并交付三个生成 Operation 的实际输出 image.png、video.mp4、narration.mp3。',
            'options':['图片：1024×768 PNG','视频：5 秒、1280×720 MP4','音频：普通话男声 MP3','三项都必须来自登记的真实生成 Operation'],
            'sources':[],'preset':{'kind':'agent','assets':[]},'expected_outputs':outputs,
            'result':'preview.png','result_type':'image/png','poster':'poster.jpg','files':['preview.png','poster.jpg'],
            'credits':[{'name':'Updream generated capability preview','url':'https://github.com/hustwujing/vj-distributed-media-edit-system',
                        'license':'Repository license','license_url':'https://github.com/hustwujing/vj-distributed-media-edit-system'}],
            'changes':'封面不冒充生成结果；载入并确认后会调用图片、视频和音频三个独立受控 Provider Operation。'}


def main():
    manifest=json.loads(MANIFEST.read_text());skills={item['id']:item for item in manifest['skills']}
    if set(skills)!=set(SPECS):raise ValueError('Native Skill example specifications do not match the frozen manifest')
    catalog=json.loads((ROOT/'catalog.json').read_text());replaced={PREFIX+name for name in SPECS}|{MEDIA_ID}
    catalog=[item for item in catalog if item['id'] not in replaced]
    catalog.extend(demo(name,skills[name],SPECS[name]) for name in SPECS)
    catalog.append(media_demo())
    if len(catalog)!=70 or len({item['id'] for item in catalog})!=70:raise ValueError('Expected exactly 70 unique examples')
    temporary=ROOT/'catalog.native-new.json';temporary.write_text(json.dumps(catalog,ensure_ascii=False,indent=2)+'\n')
    temporary.replace(ROOT/'catalog.json')
    for path in [ROOT/'catalog.json',*[ROOT/(PREFIX+name) for name in SPECS],ROOT/MEDIA_ID]:
        path.chmod(0o755 if path.is_dir() else 0o644)
        if path.is_dir():
            for child in path.iterdir():child.chmod(0o644)
    print(json.dumps({'examples':len(catalog),'native_skills':len(SPECS),'media_generation':1},ensure_ascii=False))


if __name__=='__main__':main()
