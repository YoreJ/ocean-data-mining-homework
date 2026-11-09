"""Export actual recorded user/assistant text for the two authorized chats.

System/developer messages, hidden reasoning, and tool payloads are not chat text
and are excluded. Pasted text attachments are copied from their actual files.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
CHATS = {
    "研究实现与报告对话": "01a1baed-7ab6-74c2-bd9c-84567fed9307",
    "提问生成与协作流程对话": "01a1bab7-27b0-7d41-ad2d-adc490522b64",
}


def redact_paths(text):
    text = text.replace(str(ROOT), "[项目目录]")
    text = text.replace(str(Path.home()), "[本机用户目录]")
    return text


def export(session_root):
    output = ROOT / "conversations"
    attachments = output / "attachments"
    attachments.mkdir(parents=True, exist_ok=True)
    manifest = {"exported_at_utc": datetime.now(timezone.utc).isoformat(),
                "scope": "All actual user and assistant body/progress text in the located session files, up to export time.",
                "exclusions": ["system/developer context", "hidden reasoning", "tool payloads", "automatically injected UI/environment snapshots"],
                "redactions": ["absolute local user paths replaced by explicit placeholders"],
                "chats": [], "attachments": [], "missing_attachments": []}
    known_attachments = {}
    for title, thread_id in CHATS.items():
        sources = sorted(session_root.rglob("*" + thread_id + "*.jsonl"))
        messages = []
        seen = set()
        source_records = []
        for source in sources:
            data = source.read_bytes()
            source_records.append({"filename": source.name, "sha256": hashlib.sha256(data).hexdigest()})
            for line in data.decode().splitlines():
                record = json.loads(line)
                item = record.get("payload", {})
                if record.get("type") != "response_item" or item.get("type") != "message":
                    continue
                if item.get("role") not in ("user", "assistant") or item.get("phase") == "analysis":
                    continue
                text = "\n".join(part.get("text", "") for part in item.get("content", [])
                                 if part.get("type") in ("input_text", "output_text", "text"))
                if not text or text.startswith(("<environment_context>", "<external_codex_apps_")):
                    continue
                identity = (record.get("timestamp", ""), item["role"], text)
                if identity in seen:
                    continue
                seen.add(identity)
                links = []
                for raw_path in re.findall(r"/Users/[^/]+/\.codex/attachments/[^\n]+", text):
                    candidate = Path(raw_path.strip().rstrip("`\" "))
                    if candidate.is_file():
                        key = str(candidate)
                        if key not in known_attachments:
                            content = candidate.read_text(encoding="utf-8")
                            safe = redact_paths(content)
                            name = f"pasted-{len(known_attachments)+1:02d}.md"
                            (attachments / name).write_text(safe, encoding="utf-8")
                            known_attachments[key] = name
                            manifest["attachments"].append({"file": "attachments/"+name,
                                "source_filename": candidate.name,
                                "original_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                                "export_sha256": hashlib.sha256(safe.encode()).hexdigest()})
                        links.append("attachments/" + known_attachments[key])
                    else:
                        missing = redact_paths(str(candidate))
                        if missing not in manifest["missing_attachments"]:
                            manifest["missing_attachments"].append(missing)
                messages.append({"timestamp": identity[0], "role": item["role"],
                                 "phase": item.get("phase"), "text": redact_paths(text), "attachments": links})
        messages.sort(key=lambda message: message["timestamp"])
        parts = [f"# {title}\n", "本文件从实际会话记录导出，保留用户消息、AI 正文及进度消息。\n",
                 "本机绝对路径已显式替换为占位。系统上下文、内部推理和工具载荷不作为聊天正文导出；实验请求与响应在 results 中另存。\n",
                 "粘贴附件以实际文件单独保存。当前进行中的提交轮次记录截至导出时点，之后尚未发生的回复不补造。\n",
                 f"导出时点 UTC　{manifest['exported_at_utc']}\n"]
        for number, message in enumerate(messages, 1):
            role = "用户" if message["role"] == "user" else "AI"
            if message["phase"] == "commentary":
                role += " 进度消息"
            parts += [f"\n## {number:03d}　{role}　{message['timestamp']}\n", message["text"] + "\n"]
            for attachment in message["attachments"]:
                parts.append(f"\n[实际粘贴附件全文]({attachment})\n")
        destination = output / (title + ".md")
        destination.write_text("\n".join(parts), encoding="utf-8")
        manifest["chats"].append({"title": title, "thread_id": thread_id, "file": destination.name,
            "messages": len(messages), "user_messages": sum(m['role']=='user' for m in messages),
            "assistant_messages": sum(m['role']=='assistant' for m in messages), "sources": source_records})
    (output / "export-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({"chats": [{k: c[k] for k in ("title", "messages", "user_messages", "assistant_messages")} for c in manifest['chats']],
                      "attachments": len(manifest['attachments']), "missing_attachments": manifest['missing_attachments']}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-root", type=Path, default=Path.home()/".codex/sessions")
    export(parser.parse_args().session_root)
