#!/usr/bin/env python3
"""
Test script to generate comprehensive Terraform files with all AWS services configured
"""
import requests
import json
import sys

# Prepare comprehensive test data with all fields
data = {
    'access_key': 'AKIAIOSFODNN7EXAMPLE',  # Dummy AWS credentials for testing
    'secret_key': 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
    'session_token': '',
    'aws_region': 'us-east-1',
    'services': ['vpc', 'ec2', 's3', 'rds', 'efs'],
    'service_configs': {
        'vpc': {
            'cidr': '10.0.0.0/16',
            'enable_dns_hostnames': True,
            'enable_dns_support': True,
            'enable_internet_gateway': True,
            'enable_nat_gateway': True,
            'subnets': [
                {'name': 'public-subnet-1', 'cidr': '10.0.1.0/24', 'type': 'public'},
                {'name': 'public-subnet-2', 'cidr': '10.0.2.0/24', 'type': 'public'},
                {'name': 'private-subnet-1', 'cidr': '10.0.3.0/24', 'type': 'private'},
                {'name': 'private-subnet-2', 'cidr': '10.0.4.0/24', 'type': 'private'}
            ]
        },
        'ec2': {
            # Common key pair for ALL instances (best practice)
            'key_name': 'my-keypair',
            'public_key': 'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC7X example-key',
            'instances': {
                '1': {
                    'name': 'web-server-1',
                    'ami': 'ami-0c55b159cbfafe1f0',
                    'instance_type': 't3.medium',
                    'subnet_id': '10.0.1.0/24',
                    'root_volume_size': 20,
                    'root_volume_type': 'gp3',
                    'security_group_name': 'web-server-sg',
                    'iam_role': 'web-server-role',
                    'user_data': '#!/bin/bash\necho "Hello World" > /var/www/html/index.html',
                    'tags': '{"Environment": "Production", "Team": "DevOps"}'
                },
                '2': {
                    'name': 'app-server-1',
                    'ami': 'ami-0c55b159cbfafe1f0',
                    'instance_type': 't3.large',
                    'subnet_id': '10.0.3.0/24',
                    'root_volume_size': 30,
                    'root_volume_type': 'gp3',
                    'security_group_name': 'app-server-sg',
                    'iam_role': 'app-server-role',
                    'user_data': '#!/bin/bash\necho "App Server" > /var/log/startup.log',
                    'tags': '{"Environment": "Production", "Team": "Backend"}'
                }
            },
            'additional_volumes': [
                {
                    'id': '1',
                    'name': 'web-data-volume',
                    'size': 100,
                    'type': 'gp3',
                    'iops': 3000,
                    'encrypted': True,
                    'linkedEc2': '1'
                },
                {
                    'id': '2',
                    'name': 'app-data-volume',
                    'size': 200,
                    'type': 'io2',
                    'iops': 5000,
                    'encrypted': True,
                    'linkedEc2': '2'
                }
            ]
        },
        's3': {
            'buckets': [
                {
                    'bucket_name': 'my-app-assets-bucket-12345',
                    'versioning': True,
                    'encryption': True,
                    'block_public_access': True,
                    'storage_class': 'STANDARD',
                    'enable_logging': True,
                    'lifecycle_ia_days': 30,
                    'lifecycle_glacier_days': 90,
                    'lifecycle_expiration_days': 365,
                    'enable_cors': True,
                    'tags': '{"Environment": "Production", "DataType": "Assets"}'
                },
                {
                    'bucket_name': 'my-app-backups-bucket-12345',
                    'versioning': True,
                    'encryption': True,
                    'block_public_access': True,
                    'storage_class': 'STANDARD_IA',
                    'enable_logging': False,
                    'lifecycle_ia_days': None,
                    'lifecycle_glacier_days': 30,
                    'lifecycle_expiration_days': 180,
                    'enable_cors': False,
                    'tags': '{"Environment": "Production", "DataType": "Backups"}'
                }
            ]
        },
        'rds': {
            'databases': [
                {
                    'identifier': 'prod-mysql-db',
                    'engine': 'mysql',
                    'instance_class': 'db.m5.large',
                    'allocated_storage': 100,
                    'storage_type': 'gp3',
                    'db_name': 'myappdb',
                    'username': 'admin',
                    'password': 'MySecurePassword123!',
                    'backup_retention_period': 7,
                    'security_group_name': 'mysql-db-sg',
                    'publicly_accessible': False,
                    'multi_az': True,
                    'backup_window': '03:00-04:00',
                    'maintenance_window': 'sun:04:00-sun:05:00',
                    'tags': '{"Environment": "Production", "Type": "MySQL"}'
                },
                {
                    'identifier': 'prod-postgres-db',
                    'engine': 'postgres',
                    'instance_class': 'db.m5.xlarge',
                    'allocated_storage': 200,
                    'storage_type': 'io1',
                    'db_name': 'analytics',
                    'username': 'pgadmin',
                    'password': 'PostgresSecure456!',
                    'backup_retention_period': 14,
                    'security_group_name': 'postgres-db-sg',
                    'publicly_accessible': False,
                    'multi_az': True,
                    'backup_window': '02:00-03:00',
                    'maintenance_window': 'sat:03:00-sat:04:00',
                    'tags': '{"Environment": "Production", "Type": "PostgreSQL"}'
                }
            ],
            'subnet_ids': ['10.0.3.0/24', '10.0.4.0/24'],
            'vpc_id': '10.0.0.0/16'
        },
        'efs': {
            'filesystems': [
                {
                    'name': 'shared-app-storage',
                    'performance_mode': 'generalPurpose',
                    'throughput_mode': 'bursting',
                    'storage_class': 'STANDARD',
                    'encrypted': True,  # Uses AWS-managed encryption by default
                    'enable_backup': True,
                    'transition_to_ia': 30,
                    'security_group_name': 'efs-app-sg',
                    'kms_key_id': '',  # Optional - leave empty for AWS-managed encryption
                    'tags': '{"Environment": "Production", "Purpose": "SharedStorage"}'
                },
                {
                    'name': 'ml-model-storage',
                    'performance_mode': 'maxIO',
                    'throughput_mode': 'provisioned',
                    'storage_class': 'STANDARD',
                    'encrypted': True,  # Uses AWS-managed encryption by default
                    'enable_backup': False,
                    'transition_to_ia': None,
                    'security_group_name': 'efs-ml-sg',
                    'kms_key_id': '',  # Optional - leave empty for AWS-managed encryption
                    'tags': '{"Environment": "Production", "Purpose": "MLModels"}'
                }
            ],
            'subnet_ids': ['10.0.1.0/24', '10.0.2.0/24'],
            'vpc_id': '10.0.0.0/16'
        }
    }
}

print('='*80)
print('AWS TERRAFORM GENERATOR - COMPREHENSIVE TEST')
print('='*80)
print('\nConfiguration Summary:')
print(f"  Region: {data['aws_region']}")
print(f"  Services: {', '.join(data['services'])}")
print(f"  VPC Subnets: {len(data['service_configs']['vpc']['subnets'])}")
print(f"  EC2 Instances: {len(data['service_configs']['ec2']['instances'])}")
print(f"  EBS Volumes: {len(data['service_configs']['ec2']['additional_volumes'])}")
print(f"  S3 Buckets: {len(data['service_configs']['s3']['buckets'])}")
print(f"  RDS Databases: {len(data['service_configs']['rds']['databases'])}")
print(f"  EFS Filesystems: {len(data['service_configs']['efs']['filesystems'])}")

print('\nSending request to Django backend...')

try:
    # Use the task registry API endpoint
    task_data = {
        'task_id': 'box_project_aws',
        'data': data
    }
    response = requests.post(
        'http://127.0.0.1:8000/api/tasks/run/',
        json=task_data,
        timeout=60
    )
    
    print(f'\nResponse Status: {response.status_code}')
    
    if response.status_code == 200:
        print('\nSUCCESS! Terraform files generated.')
        
        # Parse the JSON response
        result = response.json()
        print(f'\nTask result: {result.get("message", "")}')
        
        # Check for artifacts
        artifacts = result.get('artifacts', [])
        if not artifacts:
            print('\nWarning: No artifacts in response')
            print(f'Full response: {json.dumps(result, indent=2)}')
        else:
            print(f'\n{len(artifacts)} artifact(s) generated:')
            print(f'DEBUG: First artifact keys: {list(artifacts[0].keys()) if artifacts else "N/A"}')
            
            import zipfile
            import os
            import base64
            
            for idx, artifact in enumerate(artifacts, 1):
                name = artifact.get('filename', artifact.get('name', f'artifact_{idx}'))
                content_base64 = artifact.get('data', artifact.get('content', ''))
                
                print(f'\n  [{idx}] {name}')
                print(f'      Content length (base64): {len(content_base64)} chars')
                print(f'      Content type: {artifact.get("content_type", "unknown")}')
                
                if name.endswith('.zip'):
                    # Decode and save the zip file
                    content = base64.b64decode(content_base64)
                    import time
                    timestamp = int(time.time())
                    output_file = f'terraform_aws_output_{timestamp}.zip'
                    
                    with open(output_file, 'wb') as f:
                        f.write(content)
                    
                    print(f'      Saved as: {output_file}')
                    print(f'      File size: {len(content)} bytes')
                    
                    # Extract and display the contents
                    extract_dir = f'terraform_aws_output_{timestamp}'
                    if os.path.exists(extract_dir):
                        import shutil
                        shutil.rmtree(extract_dir)
                    
                    with zipfile.ZipFile(output_file, 'r') as zip_ref:
                        zip_ref.extractall(extract_dir)
                    
                    print(f'      Extracted to: {extract_dir}')
                    print('\n      Generated Files:')
                    
                    for root, dirs, files in os.walk(extract_dir):
                        level = root.replace(extract_dir, '').count(os.sep)
                        indent = ' ' * 2 * (level + 3)
                        rel_path = os.path.relpath(root, extract_dir)
                        if rel_path != '.':
                            print(f'{indent}{os.path.basename(root)}/')
                        subindent = ' ' * 2 * (level + 4)
                        for file in sorted(files):
                            file_path = os.path.join(root, file)
                            file_size = os.path.getsize(file_path)
                            print(f'{subindent}{file} ({file_size} bytes)')
        
        print('\n' + '='*80)
        print('TERRAFORM GENERATION COMPLETED SUCCESSFULLY!')
        print('='*80)
        
    else:
        print(f'\nERROR: {response.status_code}')
        print(f'Response: {response.text}')
        sys.exit(1)
        
except requests.exceptions.ConnectionError:
    print('\nERROR: Could not connect to Django backend.')
    print('   Make sure the server is running at http://127.0.0.1:8000/')
    sys.exit(1)
except Exception as e:
    print(f'\nERROR: {str(e)}')
    import traceback
    traceback.print_exc()
    sys.exit(1)

