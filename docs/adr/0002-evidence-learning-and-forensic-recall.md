# Separate Evidence Memory, Learning Memory, and Forensic Recall

fumemory separates immutable Evidence Memory from derived Learning Memory. Default recall returns Learning Memory for concise agent guidance, while explicit Forensic Recall includes Evidence Memory for proof, replay, debugging, and audit.

This avoids polluting normal agent context with raw tool traces while preserving replay-grade evidence. Learning Memory must carry source evidence links, and reflection-generated learning enters a six-hour Telegram review window before automatic integration unless the user approves, denies, or edits it sooner.
