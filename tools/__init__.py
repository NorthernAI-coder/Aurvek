# tools/__init__.py

import os
import importlib
from typing import List, Dict, Any, Callable

tools: List[Dict[str, Any]] = []
dramatiq_tasks = {}
function_handlers: Dict[str, Callable] = {}

def register_tool(tool: Dict[str, Any]):
    new_name = tool.get('function', {}).get('name', '')
    if new_name:
        for existing in tools:
            if existing.get('function', {}).get('name', '') == new_name:
                raise ValueError(
                    f"Duplicate tool name '{new_name}' in register_tool(). "
                    f"Each tool must have a unique name."
                )
    tools.append(tool)

def register_dramatiq_task(name: str, task):
    dramatiq_tasks[name] = task
    globals()[name] = task

def register_function_handler(function_name: str, handler: Callable):
    function_handlers[function_name] = handler

# Automatically import all modules in the tools folder
for filename in os.listdir(os.path.dirname(__file__)):
    if filename.endswith('.py') and filename != '__init__.py':
        module_name = filename[:-3]
        module = importlib.import_module(f'tools.{module_name}')

# Expose all Dramatiq tasks
globals().update(dramatiq_tasks)