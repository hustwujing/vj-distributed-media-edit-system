# Claude / DeepSeek 网关计数接口缺口

2026-11-04。本机当前模型路由的调查记录，不代表官方服务不提供计数。

## 初始调查结果

- 本系统Anthropic适配器以配置base（已有/v1）加/messages/count_tokens，得到标准POST /v1/messages/count_tokens；携带anthropic-version及现有认证，路径符合[Claude官方定义](https://platform.claude.com/docs/en/api/http/messages/count_tokens)。
- 对claude-opus-5及deepseek-v4.1-flash使用最小纯文本请求，Accept分别为application/json和text/event-stream，均返回相同19字节纯文本404。无/v1的候选路径也相同，未发现公开OpenAPI路由描述。
- 同一路由的两种模型均可实际完成原生工具生成。现有证据支持“网关未暴露/转发计数路由或路由访问策略拦截”的判断，不支持归因于提示词或复杂工具Schema；尚未拿到网关服务端日志，不能断言具体是哪一层。
- GPT使用另一网关，原生输入计数200；其模型目录未列出Claude或DeepSeek，不能直接将GPT计数当作这两个模型的计数。
- `native`策略下，input_token_reservation在计数不可用时拒绝正式生成；该行为保留。后续新增的显式本地估算策略见文末。直接模型提案评测不等于预算链路已经接入。

私有探测证据：.cluster/model-evals/counter-route-audit-20261104.json、counter-alternate-catalog-20261104.json，以及第二轮capability-*.json。

## 优先修复方案

在现有外部模型网关增加或放通POST /v1/messages/count_tokens，使用与生成一致的模型路由与凭据映射，完整处理system、messages、tools及图片。Claude可转发上游原生计数；DeepSeek是否提供同样端点必须按实际上游确认，不能因为采用Messages兼容协议就假设存在。

若网关需要本地计算，绑定与模型版本匹配的tokenizer和消息/工具模板，并核对多语言、工具定义、长上下文及图片与真实usage。DeepSeek提供[离线tokenizer参考](https://api-docs.deepseek.com/quick_start/token_usage/)，但不能未经核实将该示例视为当前V4.1和本机网关模板的准确计数器。

## 初始后备方案与边界

可以将生成路由与计数后端分开配置，随AgentProfile冻结计数方法、模型/tokenizer版本和摘要，接已验证的专用计数服务或本地tokenizer。若使用估算，只能明确作为有保守余量的预算预留，不冒充供应商实际用量；未配置或未通过验证的模型保持拒绝正式生成。

实际用量仍来自原始usage回执，缺失保持未知；所有预留、实际使用及偏差都可审计。不能用字符数乘固定系数、其他模型的计数、一次真实生成当作免费计数，或返回0来掩盖404。

以上为修复前的调查与方案。外部网关未由本次改造修改；后续本系统实现了显式本地预估，不能继续把“只完成定位”作为当前进度。已停止的模型对照不因该改造自动恢复。

## Claude 4.6输入回执复核

重测claude-4.6-sonnet请求前计数仍404，问题并非升级Claude 5后才出现。第一轮正式样本共586次Claude 4.6 HTTP 200响应，均有input_tokens，均无缓存读写明细；本系统保留网关报告值，但尚未独立核实其是否准确及是否包含全部缓存输入。另有223次实际有输出但output_tokens为0，这是输出回执异常证据，不能据此直接认定输入数也错误。接通计数接口前，还需验证预算账本如何处理这种可疑零输出回执，不能仅恢复请求前计数就宣称计量链路完整。

## 已授权的本地修复进度

用户随后明确要求实现本地预估。现已增加版本化策略、离线分词资产校验、逐模型系数、图片独立计量、预留/结算观测和可疑零输出回执处理。初步文本校准使用原请求SHA匹配的真实样本，Claude 5的系数独立于Claude 4.6校准。Claude 4.6、Claude 5、DS4及DS4.1已完成纯文本/单图/八图共12次真实预算链路测试；测试使用隔离SQLite，未执行业务Task，不能代替部署后的119媒体验收。校准参照网关报告的input_tokens，不独立证明该数字精确。

2026-11-04文档核实时，候选源码`8d39c6778eb6`的冻结门禁完成：Python运行900项、跳过6项，无失败；Node 59项通过。证据在`.cluster/verification/8d39c6778eb6.log`。实际部署记录仍为`deedda4df9cb`，因此新的模型选择、默认配置和计量链路尚不能宣称已经在部署实例验收。部署浏览器smoke与119媒体case仍需分别记录实际结果；后续状态以release和当轮证据为准。

后续部署更新（2026-11-04 17:05 CST）：最终版本`320c25093f7b`已部署，并以该摘要重新完成900项Python（6跳过）、59项Node门禁。模型选择、配置冻结、窄屏和低预算生成前拦截的浏览器smoke通过；默认Claude 4.6 Sonnet/high，本地估算策略已由运行服务加载。119媒体case仍暂停，未重跑；详见[部署验证记录](model-v3-deployment-validation.md)。
