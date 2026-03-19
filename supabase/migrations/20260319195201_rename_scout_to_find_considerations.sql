-- Rename 'scout' call type to 'find_considerations' in the calls table.
UPDATE calls SET call_type = 'find_considerations' WHERE call_type = 'scout';
