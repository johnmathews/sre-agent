# Conversation MCP Tools and Proxmox Output Fix

**Date:** 2026-04-06

## Context

Reviewed a recent conversation the deployed SRE agent had via the web UI. The agent was asked "What
VMs and containers are running on Proxmox?" and produced a good inventory table, but then incorrectly
stated that 3 stopped guests were "reserving 20 GiB of RAM." The user challenged this and the agent
corrected itself, but the damage was done — the initial answer was misleading.

Investigation revealed two contributing factors:
1. The `proxmox_list_guests` tool output showed allocated RAM for stopped guests in the same format as
   running guests, with no indication that stopped guests don't actually consume host resources.
2. No runbook existed explaining Proxmox resource allocation behavior for stopped vs running guests.

## Changes

### Proxmox tool output clarification (`src/agent/tools/proxmox.py`)

Stopped guests now render differently from running ones:
- Running: `+ 106 infra (VM, running) — 4 vCPU, 4.0 GiB RAM, CPU 11%`
- Stopped: `- 103 mailcow (VM, stopped) — config: 2 vCPU, 8.0 GiB RAM (not consuming host resources while stopped)`

The `config:` prefix and parenthetical caveat prevent the LLM from inferring that stopped guests
reserve or consume host memory.

### New runbook (`runbooks/proxmox-virtualization.md`)

Covers resource allocation behavior, the stopped-vs-running distinction, memory/CPU mechanics,
guest types, key metrics, and an FAQ. This gets embedded in the RAG vector store so the agent
can retrieve it for Proxmox resource questions even without calling the tool.

### Conversation history MCP tools (`src/api/mcp_server.py`)

Added two MCP-only tools for browsing past agent conversations:
- `sre_agent_list_conversations` — lists recent conversations with metadata
- `sre_agent_get_conversation` — retrieves full dialogue by session ID

These follow the existing `{service}_` naming convention so they're unambiguous when Claude Code
sees them alongside tools from other MCP servers.

### Parent CLAUDE.md (`homelab-sre/CLAUDE.md`)

Created a parent-level CLAUDE.md so that Claude Code sessions running in either `sre-agent/` or
`sre-webapp/` (with `../` added) know about the conversation MCP tools and when to use them.

## Decision: tool output vs system prompt

Considered adding guidance to the agent's system prompt about stopped guest behavior, but opted for
the tool output approach instead. The tool output is seen on every invocation and is specific to the
data being presented. System prompt guidance would be static, generic, and easy to ignore. The runbook
serves as the knowledge-base fallback.
