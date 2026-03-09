-- Enable Apache AGE extension for graph data modeling
CREATE EXTENSION IF NOT EXISTS age;

-- Load the extension into the current session
LOAD 'age';

-- Set the search path to include ag_catalog
SET search_path = ag_catalog, "$user", public;

-- Create the memU knowledge graph
SELECT create_graph('memu_graph');

-- We can create triggers or routines later to sync `memories` into `memu_graph` nodes,
-- or use the graph directly. For now, this initialization readies the system.
