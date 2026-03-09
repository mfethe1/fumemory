-- Sync trigger to keep `memories` table in sync with `memu_graph`
-- Using Apache AGE cypher queries within PL/pgSQL

-- Create the Memory vertex label first
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM ag_catalog.ag_label 
        WHERE name = 'Memory' AND graph = (SELECT oid FROM ag_catalog.ag_graph WHERE name = 'memu_graph')
    ) THEN
        PERFORM ag_catalog.create_vlabel('memu_graph', 'Memory');
    END IF;
END
$$;

-- Function to handle inserts, updates, deletes
CREATE OR REPLACE FUNCTION sync_memory_to_graph()
RETURNS TRIGGER AS $$
DECLARE
    graph_name CONSTANT varchar := 'memu_graph';
    cypher_query text;
BEGIN
    -- Apache AGE requires the ag_catalog in search path
    SET search_path = ag_catalog, "$user", public;

    IF TG_OP = 'INSERT' THEN
        cypher_query := format(
            'SELECT * FROM cypher(''%s'', $$ CREATE (v:Memory {id: "%s", type: "%s", agent_id: "%s"}) $$) AS (v agtype);',
            graph_name, NEW.id, NEW.memory_type, COALESCE(NEW.agent_id, '')
        );
        EXECUTE cypher_query;
        
        -- If parent_id exists, create an edge
        IF NEW.parent_id IS NOT NULL THEN
            cypher_query := format(
                'SELECT * FROM cypher(''%s'', $$ MATCH (child:Memory {id: "%s"}), (parent:Memory {id: "%s"}) CREATE (child)-[:DERIVED_FROM]->(parent) $$) AS (e agtype);',
                graph_name, NEW.id, NEW.parent_id
            );
            EXECUTE cypher_query;
        END IF;
        
        RETURN NEW;
        
    ELSIF TG_OP = 'UPDATE' THEN
        cypher_query := format(
            'SELECT * FROM cypher(''%s'', $$ MATCH (v:Memory {id: "%s"}) SET v.type = "%s", v.agent_id = "%s" $$) AS (v agtype);',
            graph_name, NEW.id, NEW.memory_type, COALESCE(NEW.agent_id, '')
        );
        EXECUTE cypher_query;
        
        -- Handle parent_id change (naive: delete old edge, create new)
        IF NEW.parent_id IS DISTINCT FROM OLD.parent_id THEN
            -- Delete old DERIVED_FROM edges
            cypher_query := format(
                'SELECT * FROM cypher(''%s'', $$ MATCH (child:Memory {id: "%s"})-[e:DERIVED_FROM]->() DELETE e $$) AS (e agtype);',
                graph_name, NEW.id
            );
            EXECUTE cypher_query;
            
            -- Create new edge if not null
            IF NEW.parent_id IS NOT NULL THEN
                cypher_query := format(
                    'SELECT * FROM cypher(''%s'', $$ MATCH (child:Memory {id: "%s"}), (parent:Memory {id: "%s"}) CREATE (child)-[:DERIVED_FROM]->(parent) $$) AS (e agtype);',
                    graph_name, NEW.id, NEW.parent_id
                );
                EXECUTE cypher_query;
            END IF;
        END IF;
        
        RETURN NEW;
        
    ELSIF TG_OP = 'DELETE' THEN
        cypher_query := format(
            'SELECT * FROM cypher(''%s'', $$ MATCH (v:Memory {id: "%s"}) DETACH DELETE v $$) AS (v agtype);',
            graph_name, OLD.id
        );
        EXECUTE cypher_query;
        RETURN OLD;
    END IF;
    
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- Trigger for sync
DROP TRIGGER IF EXISTS trg_sync_memory_to_graph ON memories;

CREATE TRIGGER trg_sync_memory_to_graph
AFTER INSERT OR UPDATE OR DELETE ON memories
FOR EACH ROW EXECUTE FUNCTION sync_memory_to_graph();
