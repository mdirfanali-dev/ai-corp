-- Employee profile fields and stable keys for the built-in (system) employees.

ALTER TABLE employee ADD COLUMN system_key TEXT;
CREATE UNIQUE INDEX idx_employee_system_key ON employee (system_key) WHERE system_key IS NOT NULL;
ALTER TABLE employee ADD COLUMN skills TEXT NOT NULL DEFAULT '[]';
ALTER TABLE employee ADD COLUMN profile TEXT NOT NULL DEFAULT '{}';
ALTER TABLE employee ADD COLUMN instructions TEXT NOT NULL DEFAULT '';
