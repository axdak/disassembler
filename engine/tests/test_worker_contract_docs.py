from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_autonomous_worker_prompt_requires_foreground_supervision():
    prompt = read_text("prompts/autonomous_run.j2")

    assert "主控Agent必须保持前台监督" in prompt
    assert "不得把 worker 任务后台启动后结束主会话" in prompt


def test_worker_docs_forbid_detached_background_completion():
    docs = "\n".join(
        [
            read_text("SKILL.md"),
            read_text("references/autonomous_loop.md"),
            read_text("references/commands_and_resources.md"),
        ]
    )

    assert "不得把后台任务 ID 当作完成状态" in docs
    assert "不能启动后台 worker 后结束对话" in docs


def test_worker_docs_forbid_task_notification_background_mode():
    docs = "\n".join(
        [
            read_text("SKILL.md"),
            read_text("prompts/autonomous_run.j2"),
            read_text("references/autonomous_loop.md"),
            read_text("references/commands_and_resources.md"),
        ]
    )

    assert "不得等待 task-notification" in docs
    assert "不得使用后台任务托管主流程" in docs
    assert "必须等待前台命令返回码" in docs


def test_worker_window_docs_state_default_and_visibility_limits():
    commands = read_text("references/commands_and_resources.md")

    assert "默认使用 `--worker-window hidden`" in commands
    assert "窗口不保证展示模型逐 token 交互过程" in commands
    assert "需要可见窗口时才使用 `--worker-window powershell`" in commands
def test_worker_docs_state_supervised_loop_default_and_continuous_escape_hatch():
    docs = "\n".join(
        [
            read_text("SKILL.md"),
            read_text("prompts/autonomous_run.j2"),
            read_text("references/autonomous_loop.md"),
            read_text("references/commands_and_resources.md"),
        ]
    )

    assert "\u9ed8\u8ba4 `--worker-loop supervised`" in docs
    assert "`--worker-loop continuous`" in docs
