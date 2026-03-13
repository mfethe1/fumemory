#!/usr/bin/env python3
"""
Final validation script for Graph-Lite implementation.
Checks all components without requiring external dependencies.
"""

import sys
import os
import ast

def check_models():
    """Validate models.py structure."""
    print("\n[1] Validating models.py...")
    
    models_file = os.path.join(os.path.dirname(__file__), 'memu', 'models.py')
    with open(models_file, 'r') as f:
        content = f.read()
    
    checks = [
        ('class Relationship(BaseModel):', 'Relationship model defined'),
        ('entity: str', 'Relationship.entity field'),
        ('relationship_type: str', 'Relationship.relationship_type field'),
        ('target_memory_id: Optional[UUID]', 'Relationship.target_memory_id field'),
        ('strength: float', 'Relationship.strength field'),
        ('relationships: list[Relationship]', 'MemoryCreate.relationships field'),
        ('from typing import Any, Optional', 'Optional import for Python 3.9 compatibility'),
    ]
    
    all_passed = True
    for check_str, description in checks:
        if check_str in content:
            print(f"  ✓ {description}")
        else:
            print(f"  ✗ Missing: {description}")
            all_passed = False
    
    # Check for Python 3.9 incompatible syntax
    incompatible = [
        ('UUID | None', 'Should use Optional[UUID] instead of UUID | None'),
        ('str | None', 'Should use Optional[str] instead of str | None'),
        ('dict | None', 'Should use Optional[dict] instead of dict | None'),
    ]
    
    for bad_syntax, message in incompatible:
        if bad_syntax in content:
            print(f"  ⚠ Warning: {message}")
            all_passed = False
    
    return all_passed


def check_api():
    """Validate api.py implementation."""
    print("\n[2] Validating api.py...")
    
    api_file = os.path.join(os.path.dirname(__file__), 'memu', 'api.py')
    with open(api_file, 'r') as f:
        content = f.read()
    
    checks = [
        ('if req.relationships:', 'Checks for relationships in request'),
        ('for rel in req.relationships:', 'Iterates over relationships'),
        ('if rel.target_memory_id:', 'Handles target_memory_id'),
        ('INSERT INTO memory_links', 'Inserts into memory_links table'),
        ('rel.relationship_type', 'Uses relationship_type'),
        ('rel.strength', 'Uses strength'),
        ('rel.entity', 'Uses entity'),
        ('ON CONFLICT', 'Handles duplicate relationships'),
    ]
    
    all_passed = True
    for check_str, description in checks:
        if check_str in content:
            print(f"  ✓ {description}")
        else:
            print(f"  ✗ Missing: {description}")
            all_passed = False
    
    return all_passed


def check_migration():
    """Validate migration file."""
    print("\n[3] Validating migration...")
    
    migration_file = os.path.join(
        os.path.dirname(__file__), 
        'memu', 
        'migrations', 
        '002_amem_bitemporal.sql'
    )
    
    with open(migration_file, 'r') as f:
        content = f.read()
    
    checks = [
        ('CREATE TABLE IF NOT EXISTS memory_links', 'memory_links table'),
        ('source_id     UUID NOT NULL REFERENCES memories(id)', 'source_id foreign key'),
        ('target_id     UUID NOT NULL REFERENCES memories(id)', 'target_id foreign key'),
        ('relationship  VARCHAR(20)', 'relationship column'),
        ('strength      FLOAT', 'strength column'),
        ('metadata      JSONB', 'metadata column'),
        ('UNIQUE(source_id, target_id, relationship)', 'unique constraint'),
        ('CREATE INDEX IF NOT EXISTS idx_links_source', 'source index'),
        ('CREATE INDEX IF NOT EXISTS idx_links_target', 'target index'),
    ]
    
    all_passed = True
    for check_str, description in checks:
        if check_str in content:
            print(f"  ✓ {description}")
        else:
            print(f"  ✗ Missing: {description}")
            all_passed = False
    
    return all_passed


def check_documentation():
    """Validate documentation exists."""
    print("\n[4] Validating documentation...")
    
    docs = [
        ('docs/GRAPH_LITE_RELATIONSHIPS.md', 'Feature documentation'),
        ('GRAPH_LITE_IMPLEMENTATION.md', 'Implementation summary'),
        ('eval_graph_lite.py', 'Evaluation script'),
        ('tests/test_graph_lite_relationships.py', 'Database tests'),
        ('tests/test_graph_lite_api.py', 'API tests'),
    ]
    
    all_passed = True
    for filepath, description in docs:
        full_path = os.path.join(os.path.dirname(__file__), filepath)
        if os.path.exists(full_path):
            print(f"  ✓ {description}")
        else:
            print(f"  ✗ Missing: {description}")
            all_passed = False
    
    return all_passed


def main():
    """Run all validation checks."""
    print("=" * 80)
    print("Graph-Lite Implementation Validation")
    print("=" * 80)
    
    results = []
    results.append(("Models", check_models()))
    results.append(("API", check_api()))
    results.append(("Migration", check_migration()))
    results.append(("Documentation", check_documentation()))
    
    print("\n" + "=" * 80)
    print("Summary")
    print("=" * 80)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} - {name}")
    
    print(f"\nTotal: {passed}/{total} checks passed")
    
    if passed == total:
        print("\n✅ Graph-Lite implementation is complete and valid!")
        print("\nNext steps:")
        print("  1. Start PostgreSQL database")
        print("  2. Run: python3 fumemory/eval_graph_lite.py")
        print("  3. Run: python3 -m pytest fumemory/tests/test_graph_lite_*.py")
        return 0
    else:
        print(f"\n❌ {total - passed} check(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())

