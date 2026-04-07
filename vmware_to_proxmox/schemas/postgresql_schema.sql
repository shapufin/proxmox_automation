-- PostgreSQL Schema for Production-Ready VMware-to-Proxmox Migration Framework
-- Supports task graph DAG, checkpointing, and hardware fidelity

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Hardware specification tables
CREATE TABLE hardware_specs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    vm_name VARCHAR(255) NOT NULL,
    vmware_hardware_version VARCHAR(50),
    cpu_sockets INTEGER NOT NULL,
    cpu_cores_per_socket INTEGER NOT NULL,
    cpu_threads_per_core INTEGER NOT NULL,
    cpu_topology VARCHAR(20) NOT NULL CHECK (cpu_topology IN ('sockets', 'cores', 'threads')),
    cpu_features JSONB DEFAULT '[]',
    cpu_numa_nodes JSONB,
    memory_mb INTEGER NOT NULL,
    firmware_type VARCHAR(10) NOT NULL CHECK (firmware_type IN ('bios', 'uefi')),
    firmware_secure_boot BOOLEAN DEFAULT FALSE,
    firmware_tpm_present BOOLEAN DEFAULT FALSE,
    firmware_efi_vars JSONB,
    snapshot_count INTEGER DEFAULT 0,
    has_encryption BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE network_interfaces (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    hardware_spec_id UUID NOT NULL REFERENCES hardware_specs(id) ON DELETE CASCADE,
    label VARCHAR(100) NOT NULL,
    mac_address_original VARCHAR(17) NOT NULL,
    mac_address_preserved BOOLEAN DEFAULT TRUE,
    mac_address_generated VARCHAR(17),
    network_name VARCHAR(100) NOT NULL,
    adapter_type VARCHAR(50) NOT NULL,
    vlan_id INTEGER,
    connected BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE disk_controllers (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    controller_type VARCHAR(50) NOT NULL,
    bus_number INTEGER NOT NULL,
    scsi_controller INTEGER,
    unit_number INTEGER
);

CREATE TABLE disk_specs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    hardware_spec_id UUID NOT NULL REFERENCES hardware_specs(id) ON DELETE CASCADE,
    controller_id UUID NOT NULL REFERENCES disk_controllers(id),
    label VARCHAR(100) NOT NULL,
    capacity_bytes BIGINT NOT NULL,
    backing_type VARCHAR(50) NOT NULL,
    thin_provisioned BOOLEAN NOT NULL,
    file_name VARCHAR(500) NOT NULL,
    hardware_version VARCHAR(50),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE passthrough_devices (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    hardware_spec_id UUID NOT NULL REFERENCES hardware_specs(id) ON DELETE CASCADE,
    device_type VARCHAR(10) NOT NULL CHECK (device_type IN ('pci', 'usb', 'iso')),
    vendor_id VARCHAR(4) NOT NULL,
    device_id VARCHAR(4) NOT NULL,
    subsystem_vendor_id VARCHAR(4),
    subsystem_device_id VARCHAR(4),
    address VARCHAR(100) NOT NULL,
    description TEXT,
    required BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Migration jobs with enhanced tracking
CREATE TYPE job_status AS ENUM ('pending', 'preflight', 'running', 'paused', 'completed', 'failed', 'rolled_back');
CREATE TYPE migration_mode AS ENUM ('vmware_direct', 'local_disks', 'archive_import');

CREATE TABLE migration_jobs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL,
    mode migration_mode NOT NULL DEFAULT 'local_disks',
    hardware_spec_id UUID NOT NULL REFERENCES hardware_specs(id),
    
    -- Target configuration
    proxmox_host_id UUID,
    vmware_host_id UUID,
    target_node VARCHAR(100),
    target_storage VARCHAR(100),
    target_bridge VARCHAR(100),
    target_vmid INTEGER,
    
    -- Migration settings
    disk_format VARCHAR(10) DEFAULT 'qcow2' CHECK (disk_format IN ('qcow2', 'raw')),
    dry_run BOOLEAN DEFAULT TRUE,
    start_after_import BOOLEAN DEFAULT TRUE,
    ssh_bandwidth_limit_mbps INTEGER,
    
    -- Status tracking
    status job_status DEFAULT 'pending',
    progress_percentage INTEGER DEFAULT 0 CHECK (progress_percentage >= 0 AND progress_percentage <= 100),
    current_task_id UUID,
    
    -- Checkpointing data
    checkpoint_data JSONB DEFAULT '{}',
    resume_count INTEGER DEFAULT 0,
    
    -- Result data
    result JSONB DEFAULT '{}',
    error_message TEXT,
    error_traceback TEXT,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    started_at TIMESTAMP WITH TIME ZONE,
    finished_at TIMESTAMP WITH TIME ZONE,
    
    -- Optimizations
    CONSTRAINT valid_vmid CHECK (target_vmid IS NULL OR (target_vmid >= 100 AND target_vmid <= 999999999))
);

-- Task graph DAG system
CREATE TYPE task_type AS ENUM (
    'preflight_check', 'snapshot_consolidate', 'disk_export', 'disk_transfer', 
    'disk_import', 'vm_creation', 'disk_attachment', 'network_config',
    'guest_remediation', 'vm_start', 'post_migration_check', 'cleanup'
);
CREATE TYPE task_status AS ENUM ('pending', 'running', 'completed', 'failed', 'skipped', 'paused');

CREATE TABLE migration_tasks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID NOT NULL REFERENCES migration_jobs(id) ON DELETE CASCADE,
    
    -- Task definition
    task_type task_type NOT NULL,
    task_name VARCHAR(255) NOT NULL,
    description TEXT,
    
    -- DAG dependencies
    depends_on UUID[] DEFAULT '{}', -- Array of task IDs this task depends on
    priority INTEGER DEFAULT 0, -- Higher priority runs first
    
    -- Execution data
    status task_status DEFAULT 'pending',
    progress_percentage INTEGER DEFAULT 0 CHECK (progress_percentage >= 0 AND progress_percentage <= 100),
    
    -- Checkpointing support
    checkpoint_data JSONB DEFAULT '{}',
    is_resumable BOOLEAN DEFAULT TRUE,
    
    -- Retry logic
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    retry_delay_seconds INTEGER DEFAULT 30,
    
    -- Timing
    started_at TIMESTAMP WITH TIME ZONE,
    completed_at TIMESTAMP WITH TIME ZONE,
    estimated_duration_seconds INTEGER,
    actual_duration_seconds INTEGER,
    
    -- Results
    result JSONB DEFAULT '{}',
    error_message TEXT,
    error_traceback TEXT,
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    CONSTRAINT valid_status CHECK (
        (status = 'pending' AND started_at IS NULL) OR
        (status = 'running' AND started_at IS NOT NULL AND completed_at IS NULL) OR
        (status IN ('completed', 'failed', 'skipped') AND started_at IS NOT NULL AND completed_at IS NOT NULL) OR
        (status = 'paused' AND started_at IS NOT NULL AND completed_at IS NULL)
    )
);

-- Pre-flight check results
CREATE TABLE preflight_checks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID NOT NULL REFERENCES migration_jobs(id) ON DELETE CASCADE,
    
    check_name VARCHAR(255) NOT NULL,
    check_category VARCHAR(100) NOT NULL, -- 'hardware', 'network', 'storage', 'compatibility'
    status VARCHAR(20) NOT NULL CHECK (status IN ('passed', 'warning', 'failed', 'skipped')),
    
    check_details JSONB DEFAULT '{}',
    recommendation TEXT,
    is_blocking BOOLEAN DEFAULT FALSE, -- If failed, blocks migration
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Checkpoint tracking for large file transfers
CREATE TABLE transfer_checkpoints (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id UUID NOT NULL REFERENCES migration_tasks(id) ON DELETE CASCADE,
    
    source_path VARCHAR(1000) NOT NULL,
    target_path VARCHAR(1000) NOT NULL,
    total_bytes BIGINT NOT NULL,
    transferred_bytes BIGINT DEFAULT 0,
    
    -- Transfer metadata
    checksum_source VARCHAR(64),
    checksum_target VARCHAR(64),
    transfer_method VARCHAR(50), -- 'rsync', 'dd', 'qemu-img'
    
    -- Bandwidth tracking
    bandwidth_limit_mbps INTEGER,
    average_bps BIGINT,
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    CONSTRAINT valid_progress CHECK (transferred_bytes >= 0 AND transferred_bytes <= total_bytes)
);

-- Audit trail
CREATE TABLE audit_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    
    -- Entity tracking
    entity_type VARCHAR(50) NOT NULL, -- 'job', 'task', 'hardware_spec'
    entity_id UUID NOT NULL,
    
    -- Action tracking
    action VARCHAR(100) NOT NULL, -- 'created', 'updated', 'started', 'completed', 'failed'
    actor VARCHAR(255), -- User or system component
    
    -- State changes
    old_values JSONB,
    new_values JSONB,
    
    -- Metadata
    ip_address INET,
    user_agent TEXT,
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Performance indexes
CREATE INDEX idx_migration_jobs_status ON migration_jobs(status);
CREATE INDEX idx_migration_jobs_created_at ON migration_jobs(created_at DESC);
CREATE INDEX idx_migration_tasks_job_id ON migration_tasks(job_id);
CREATE INDEX idx_migration_tasks_status ON migration_tasks(status);
CREATE INDEX idx_migration_tasks_type ON migration_tasks(task_type);
CREATE INDEX idx_migration_tasks_depends_on ON migration_tasks USING GIN(depends_on);
CREATE INDEX idx_hardware_specs_vm_name ON hardware_specs(vm_name);
CREATE INDEX idx_audit_logs_entity ON audit_logs(entity_type, entity_id);
CREATE INDEX idx_audit_logs_created_at ON audit_logs(created_at DESC);
CREATE INDEX idx_transfer_checkpoints_task_id ON transfer_checkpoints(task_id);

-- Triggers for updated_at timestamps
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_hardware_specs_updated_at BEFORE UPDATE ON hardware_specs FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_migration_jobs_updated_at BEFORE UPDATE ON migration_jobs FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_migration_tasks_updated_at BEFORE UPDATE ON migration_tasks FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_transfer_checkpoints_updated_at BEFORE UPDATE ON transfer_checkpoints FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Trigger for audit logging
CREATE OR REPLACE FUNCTION audit_trigger_function()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO audit_logs (entity_type, entity_id, action, old_values, new_values)
    VALUES (
        TG_TABLE_NAME,
        COALESCE(NEW.id, OLD.id),
        TG_OP,
        CASE WHEN TG_OP = 'DELETE' THEN row_to_json(OLD) ELSE NULL END,
        CASE WHEN TG_OP IN ('INSERT', 'UPDATE') THEN row_to_json(NEW) ELSE NULL END
    );
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_migration_jobs AFTER INSERT OR UPDATE OR DELETE ON migration_jobs FOR EACH ROW EXECUTE FUNCTION audit_trigger_function();
CREATE TRIGGER audit_migration_tasks AFTER INSERT OR UPDATE OR DELETE ON migration_tasks FOR EACH ROW EXECUTE FUNCTION audit_trigger_function();
