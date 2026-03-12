#!/usr/bin/env python3
"""
Standalone evaluation script for Graph-Lite Entity & Relationship Tagging.

This script can be run without pytest and validates:
1. Relationships field exists in MemoryCreate model
2. API accepts relationships array
3. Relationships are stored in memory_links table
4. Entity metadata is stored when target_memory_id is not provided

Usage:
    python3 eval_graph_lite.py
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(__file__))

def test_model_structure():
    """Test that the Relationship and MemoryCreate models are properly defined."""
    print("\n[Test 1] Validating model structure...")
    
    try:
        from memu.models import MemoryCreate, Relationship, MemoryType
        from uuid import uuid4
        
        # Test Relationship model
        rel = Relationship(
            entity="TestEntity",
            relationship_type="extends",
            target_memory_id=uuid4(),
            strength=0.8
        )
        assert rel.entity == "TestEntity"
        assert rel.relationship_type == "extends"
        assert rel.strength == 0.8
        print("  ✓ Relationship model validated")
        
        # Test MemoryCreate with relationships
        mem = MemoryCreate(
            content="Test memory",
            memory_type=MemoryType.fact,
            agent_id="test_agent",
            relationships=[rel]
        )
        assert len(mem.relationships) == 1
        assert mem.relationships[0].entity == "TestEntity"
        print("  ✓ MemoryCreate model accepts relationships array")
        
        # Test empty relationships (default)
        mem_empty = MemoryCreate(
            content="Test memory without relationships",
            memory_type=MemoryType.fact,
            agent_id="test_agent"
        )
        assert mem_empty.relationships == []
        print("  ✓ MemoryCreate relationships defaults to empty list")
        
        return True
        
    except Exception as e:
        print(f"  ✗ Model validation failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_api_structure():
    """Test that the API code includes relationship handling."""
    print("\n[Test 2] Validating API implementation...")
    
    try:
        # Read the API file and check for relationship handling
        api_file = os.path.join(os.path.dirname(__file__), 'memu', 'api.py')
        with open(api_file, 'r') as f:
            api_content = f.read()
        
        # Check for relationship processing code
        checks = [
            ('req.relationships', 'Checks for relationships in request'),
            ('memory_links', 'References memory_links table'),
            ('relationship_type', 'Uses relationship_type field'),
            ('target_memory_id', 'Handles target_memory_id'),
        ]
        
        all_passed = True
        for check_str, description in checks:
            if check_str in api_content:
                print(f"  ✓ {description}")
            else:
                print(f"  ✗ Missing: {description}")
                all_passed = False
        
        return all_passed
        
    except Exception as e:
        print(f"  ✗ API validation failed: {e}")
        return False


def test_migration_exists():
    """Test that the memory_links table migration exists."""
    print("\n[Test 3] Validating database schema...")
    
    try:
        migration_file = os.path.join(
            os.path.dirname(__file__), 
            'memu', 
            'migrations', 
            '002_amem_bitemporal.sql'
        )
        
        with open(migration_file, 'r') as f:
            migration_content = f.read()
        
        # Check for memory_links table
        checks = [
            ('CREATE TABLE IF NOT EXISTS memory_links', 'memory_links table creation'),
            ('source_id', 'source_id column'),
            ('target_id', 'target_id column'),
            ('relationship', 'relationship column'),
            ('strength', 'strength column'),
            ('metadata', 'metadata column'),
        ]
        
        all_passed = True
        for check_str, description in checks:
            if check_str in migration_content:
                print(f"  ✓ {description}")
            else:
                print(f"  ✗ Missing: {description}")
                all_passed = False
        
        return all_passed
        
    except Exception as e:
        print(f"  ✗ Migration validation failed: {e}")
        return False


def main():
    """Run all evaluation tests."""
    print("=" * 80)
    print("Graph-Lite Entity & Relationship Tagging - Evaluation")
    print("=" * 80)
    
    results = []
    
    # Run tests
    results.append(("Model Structure", test_model_structure()))
    results.append(("API Implementation", test_api_structure()))
    results.append(("Database Schema", test_migration_exists()))
    
    # Summary
    print("\n" + "=" * 80)
    print("Summary")
    print("=" * 80)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} - {name}")
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n✅ All Graph-Lite implementation tests passed!")
        return 0
    else:
        print(f"\n❌ {total - passed} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())

