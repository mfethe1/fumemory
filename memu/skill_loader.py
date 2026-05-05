"""Progressive Skill Loading - Lazy-load tools/skills to reduce context window size.

This module implements a lazy-loading mechanism for skills and tools in worker loops.
Instead of loading all skills upfront, skills are loaded on-demand when needed,
reducing memory footprint and context window size.

Author: Lenny
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Dict, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Registry for lazy-loading skills and tools.
    
    Skills are only loaded when first accessed, reducing initial memory footprint
    and context window size in worker loops.
    """
    
    def __init__(self, skill_dirs: list[str | Path] | None = None):
        """Initialize the skill registry.
        
        Args:
            skill_dirs: List of directories to search for skills.
                       Defaults to ['skills/'] relative to project root.
        """
        self._skills: Dict[str, Any] = {}
        self._skill_metadata: Dict[str, dict] = {}
        self._loaded_skills: set[str] = set()
        
        if skill_dirs is None:
            skill_dirs = [Path(__file__).parent.parent / "skills"]
        
        self.skill_dirs = [Path(d) for d in skill_dirs]
        self._discover_skills()
    
    def _discover_skills(self) -> None:
        """Discover available skills without loading them."""
        for skill_dir in self.skill_dirs:
            if not skill_dir.exists():
                logger.warning(f"Skill directory not found: {skill_dir}")
                continue
            
            for skill_path in skill_dir.iterdir():
                if not skill_path.is_dir():
                    continue
                
                skill_md = skill_path / "SKILL.md"
                if skill_md.exists():
                    skill_name = skill_path.name
                    self._skill_metadata[skill_name] = {
                        "path": skill_path,
                        "description_file": skill_md,
                        "loaded": False,
                    }
                    logger.debug(f"Discovered skill: {skill_name}")
    
    def get_skill(self, skill_name: str) -> Optional[Any]:
        """Lazy-load and return a skill by name.
        
        Args:
            skill_name: Name of the skill to load.
            
        Returns:
            The loaded skill module or None if not found.
        """
        # Return cached skill if already loaded
        if skill_name in self._loaded_skills:
            return self._skills.get(skill_name)
        
        # Check if skill exists in metadata
        if skill_name not in self._skill_metadata:
            logger.warning(f"Skill not found: {skill_name}")
            return None
        
        # Lazy-load the skill
        try:
            skill_meta = self._skill_metadata[skill_name]
            skill_path = skill_meta["path"]
            
            # Try to import the skill module if it has a Python entry point
            init_file = skill_path / "__init__.py"
            if init_file.exists():
                module_name = f"skills.{skill_name}"
                skill_module = importlib.import_module(module_name)
                self._skills[skill_name] = skill_module
            else:
                # For non-Python skills, just store metadata
                self._skills[skill_name] = skill_meta
            
            self._loaded_skills.add(skill_name)
            skill_meta["loaded"] = True
            logger.info(f"Loaded skill: {skill_name}")
            
            return self._skills[skill_name]
        
        except Exception as e:
            logger.error(f"Failed to load skill {skill_name}: {e}")
            return None
    
    def list_available_skills(self) -> list[str]:
        """List all available skills (without loading them).
        
        Returns:
            List of skill names.
        """
        return list(self._skill_metadata.keys())
    
    def list_loaded_skills(self) -> list[str]:
        """List currently loaded skills.
        
        Returns:
            List of loaded skill names.
        """
        return list(self._loaded_skills)
    
    def unload_skill(self, skill_name: str) -> bool:
        """Unload a skill to free memory.
        
        Args:
            skill_name: Name of the skill to unload.
            
        Returns:
            True if skill was unloaded, False otherwise.
        """
        if skill_name in self._loaded_skills:
            self._skills.pop(skill_name, None)
            self._loaded_skills.remove(skill_name)
            if skill_name in self._skill_metadata:
                self._skill_metadata[skill_name]["loaded"] = False
            logger.info(f"Unloaded skill: {skill_name}")
            return True
        return False


# Global skill registry instance
_global_registry: Optional[SkillRegistry] = None


def get_skill_registry() -> SkillRegistry:
    """Get or create the global skill registry.
    
    Returns:
        The global SkillRegistry instance.
    """
    global _global_registry
    if _global_registry is None:
        _global_registry = SkillRegistry()
    return _global_registry

