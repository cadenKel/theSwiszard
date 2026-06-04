"""Integration: agent loop honors the safety gate."""
from __future__ import annotations

from swiszcli.agent import Agent, AgentState

BT = chr(96)


def _make_chat_stream(replies):
	rs = iter(replies)
	def chat(messages):
		text = next(rs)
		for ch in text:
			yield ch
	return chat


def test_destructive_blocked_when_no_confirm():
	executed = []
	def swiszard(task):
		executed.append(task)
		return "ok"
	replies = [
		f"<<SWISZ>>run {BT}rm -rf /home/ziggibot/junk{BT}<<END>>",
		"final",
	]
	agent = Agent(
		state=AgentState(system_prompt="sys"),
		chat_stream=_make_chat_stream(replies),
		swiszard_do=swiszard,
		on_token=lambda s: None,
	)
	out = agent.turn("kill it")
	assert executed == [], "destructive task should not have run"
	# The result fed back must say BLOCKED
	results_turn = agent.state.history[-2]
	assert "BLOCKED" in results_turn.content
	assert "rm -rf" in results_turn.content


def test_destructive_runs_when_confirm_yes():
	executed = []
	def swiszard(task):
		executed.append(task)
		return "ran"
	replies = [
		f"<<SWISZ>>run {BT}rm -rf /home/ziggibot/junk{BT}<<END>>",
		"final",
	]
	prompts = []
	def confirm(task, verdict):
		prompts.append((task, verdict.reasons))
		return True
	agent = Agent(
		state=AgentState(system_prompt="sys"),
		chat_stream=_make_chat_stream(replies),
		swiszard_do=swiszard,
		on_token=lambda s: None,
		confirm_destructive=confirm,
	)
	agent.turn("kill it")
	assert len(executed) == 1
	assert len(prompts) == 1
	assert "rm -rf" in prompts[0][1]


def test_destructive_blocked_when_confirm_no():
	executed = []
	def swiszard(task):
		executed.append(task)
		return "ran"
	replies = [
		f"<<SWISZ>>run {BT}sudo rm /etc/passwd{BT}<<END>>",
		"final",
	]
	agent = Agent(
		state=AgentState(system_prompt="sys"),
		chat_stream=_make_chat_stream(replies),
		swiszard_do=swiszard,
		on_token=lambda s: None,
		confirm_destructive=lambda t, v: False,
	)
	agent.turn("do it")
	assert executed == []
	assert "BLOCKED" in agent.state.history[-2].content
	assert "declined" in agent.state.history[-2].content


def test_safe_task_runs_without_confirm():
	executed = []
	def swiszard(task):
		executed.append(task)
		return "files"
	replies = [
		f"<<SWISZ>>run {BT}ls /tmp{BT}<<END>>",
		"final",
	]
	confirm_called = []
	def confirm(t, v):
		confirm_called.append(t)
		return True
	agent = Agent(
		state=AgentState(system_prompt="sys"),
		chat_stream=_make_chat_stream(replies),
		swiszard_do=swiszard,
		on_token=lambda s: None,
		confirm_destructive=confirm,
	)
	agent.turn("list tmp")
	assert len(executed) == 1
	assert confirm_called == [], "safe task should not invoke confirm"


def test_safety_prefix_bypasses_gate():
	executed = []
	def swiszard(task):
		executed.append(task)
		return "preview"
	replies = [
		f"<<SWISZ>>safety: run {BT}rm -rf /{BT}<<END>>",
		"final",
	]
	agent = Agent(
		state=AgentState(system_prompt="sys"),
		chat_stream=_make_chat_stream(replies),
		swiszard_do=swiszard,
		on_token=lambda s: None,
	)
	agent.turn("preview the nuke")
	assert len(executed) == 1
