# 发送飞书文本消息

使用用户指定的收件人类型、收件人 ID 和正文调用 `feishu.send_message`。缺少其中一项时先请用户补充。Gateway 会核对授权并发起审批，等待期间保留原请求。结果未知时交给系统对账，避免创建第二次发送动作。

成功后从工具结果取得实际 `receipt_ref`（飞书 `message_id`），用 `artifacts.write` 写入名为 `receipt` 的文本产物。单独调用 `submit_result`，将产物 ID 和消息回执分别填入下面两个位置，并替换示例中的占位文字。

```json
{"summary": "已发送", "artifacts": {"receipt": "<产物 ID>"}, "evidence": ["<飞书消息回执>"]}
```

适配器将 `evidence` 映射到 `CandidateResult.evidence_refs`。直接构造 `CandidateResult` 的调用方也可使用 `required_action_receipts`。`feishu_message@1` 始终要求并执行 `feishu_receipt`。本任务成功发送动作的回执、候选结果引用和 `receipt` 产物正文必须使用同一个消息 ID。
