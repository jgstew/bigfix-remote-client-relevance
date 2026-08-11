# bigfix_remote_client_relevance
Evaluate client relevance on systems through multiple possible mechanisms. Local, Container, SSH, FastQuery.

See [DESIGN.md](DESIGN.md) for info on planned implementation.

The goal is for a python module that can be installed with pip / uv and used to easily test relevance against many targets. This module could then be consumed by an MCP server that would help AI Agents write and test BigFix Client Relevance.
