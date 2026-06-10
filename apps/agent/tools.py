MEMORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_workspace",
            "description": "Creates a structural folder or root workspace to organize memories and research. Use this to set up a clean hierarchy before saving data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The human-readable name of the workspace or folder (e.g., 'Quantum_Hardware_2026')."},
                    "parent_id": {"type": "string", "description": "The node_id of the parent workspace. Omit or pass null if creating a top-level root workspace."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Saves a piece of extracted knowledge, research, or text into a specific workspace. The system will automatically generate semantic embeddings for future search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "workspace_node_id": {"type": "string", "description": "The node_id of the workspace or folder where this memory should be saved."},
                    "content_text": {"type": "string", "description": "The actual knowledge, summary, or text to remember."},
                    "name": {"type": "string", "description": "A short, descriptive title for this specific memory node."},
                },
                "required": ["workspace_node_id", "content_text", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "Executes a semantic vector search to find relevant memories or research previously saved. Always use this to recall facts before writing a final report.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query_text": {"type": "string", "description": "The natural language question or topic to search for (e.g., 'What are the leading error correction protocols?')."},
                    "scope_node_id": {
                        "type": "string",
                        "description": "The node_id of the workspace to limit the search to. If you want to search globally across all workspaces, omit this or pass null.",
                    },
                    "top_k": {"type": "integer", "description": "The maximum number of relevant memories to retrieve. Defaults to 5."},
                },
                "required": ["query_text"],
            },
        },
    },
]
