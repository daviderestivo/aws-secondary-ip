#!/usr/bin/env python3
import boto3
import subprocess
import sys
import argparse
import time
import random
from datetime import datetime
from jinja2 import Template
import ipaddress

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning, module="boto3")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="botocore")
warnings.filterwarnings("ignore", message=".*Boto3 will no longer support Python.*")

def ping_instance(ip):
    """Ping instance and return True if reachable"""
    try:
        print(f"  DEBUG: Starting ping to {ip}...")
        result = subprocess.run(['ping', '-c', '1', '-W', '2', ip],
                              capture_output=True, text=True, timeout=5)
        print(f"  DEBUG: Ping completed with return code: {result.returncode}")
        print(f"  DEBUG: Ping stdout: {result.stdout}")
        print(f"  DEBUG: Ping stderr: {result.stderr}")
        if result.returncode == 0:
            print(f"  PING {ip}: SUCCESS")
            return True
        else:
            print(f"  PING {ip}: FAILED")
            return False
    except subprocess.TimeoutExpired:
        print(f"  PING {ip}: TIMEOUT")
        return False
    except Exception as e:
        print(f"  PING {ip}: ERROR - {e}")
        return False

def get_instance_info(instance_id):
    """Get instance IP and AZ"""
    from botocore.config import Config
    config = Config(
        read_timeout=10,
        connect_timeout=10,
        retries={'max_attempts': 2}
    )
    ec2 = boto3.client('ec2', config=config)
    try:
        print(f"  DEBUG: Checking instance {instance_id}...")
        response = ec2.describe_instances(InstanceIds=[instance_id])
        instance = response['Reservations'][0]['Instances'][0]

        return {
            'ip': instance.get('PrivateIpAddress'),
            'az': instance['Placement']['AvailabilityZone'],
            'state': instance['State']['Name'],
            'ami_id': instance['ImageId'],
            'instance_type': instance['InstanceType'],
            'subnet_id': instance['SubnetId']
        }
    except Exception as e:
        print(f"  DEBUG: Error getting instance info: {e}")
        return None

def get_subnet_gateway(subnet_id):
    """Get default gateway for subnet"""
    ec2 = boto3.client('ec2')
    subnet = ec2.describe_subnets(SubnetIds=[subnet_id])['Subnets'][0]
    cidr = subnet['CidrBlock']
    network = ipaddress.IPv4Network(cidr)
    return str(network.network_address + 1)  # First usable IP is gateway

def get_other_azs(current_az):
    """Get other AZs in the same region"""
    ec2 = boto3.client('ec2')
    azs = ec2.describe_availability_zones()['AvailabilityZones']
    return [az['ZoneName'] for az in azs if az['ZoneName'] != current_az]

def get_subnet_in_az(target_az, original_subnet_id):
    """Get a subnet in the target AZ"""
    ec2 = boto3.client('ec2')

    # Get VPC ID from original subnet
    subnets = ec2.describe_subnets(SubnetIds=[original_subnet_id])
    vpc_id = subnets['Subnets'][0]['VpcId']

    # Find subnet in target AZ
    subnets = ec2.describe_subnets(
        Filters=[
            {'Name': 'vpc-id', 'Values': [vpc_id]},
            {'Name': 'availability-zone', 'Values': [target_az]}
        ]
    )

    if subnets['Subnets']:
        return subnets['Subnets'][0]['SubnetId']
    return None

def get_route_tables(subnet_id):
    """Get route tables associated with subnet"""
    ec2 = boto3.client('ec2')

    # Get VPC ID from subnet
    subnets = ec2.describe_subnets(SubnetIds=[subnet_id])
    vpc_id = subnets['Subnets'][0]['VpcId']

    # Get route tables for VPC
    route_tables = ec2.describe_route_tables(
        Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}]
    )

    return [rt['RouteTableId'] for rt in route_tables['RouteTables']]

def update_routes(old_instance_id, new_instance_id, destination_cidr):
    """Update static routes from old to new instance"""
    ec2 = boto3.client('ec2')

    # Get route tables - use new instance's subnet if old doesn't exist
    old_info = get_instance_info(old_instance_id)
    if old_info:
        subnet_id = old_info['subnet_id']
    else:
        # Use new instance's subnet
        new_info = get_instance_info(new_instance_id)
        subnet_id = new_info['subnet_id']

    route_table_ids = get_route_tables(subnet_id)

    for rt_id in route_table_ids:
        try:
            # Delete old route
            ec2.delete_route(
                RouteTableId=rt_id,
                DestinationCidrBlock=destination_cidr
            )
            print(f"Deleted route {destination_cidr} from route table {rt_id}")
        except:
            pass  # Route might not exist

        try:
            # Add new route
            ec2.create_route(
                RouteTableId=rt_id,
                DestinationCidrBlock=destination_cidr,
                InstanceId=new_instance_id
            )
            print(f"Added route {destination_cidr} -> {new_instance_id} in route table {rt_id}")
        except Exception as e:
            print(f"Failed to add route in {rt_id}: {e}")

def render_user_data(template_file, az_subnet_gateway, route_destination):
    """Render Jinja2 template with variables"""
    with open(template_file, 'r') as f:
        template_content = f.read()

    template = Template(template_content)
    return template.render(
        AZ_SUBNET_DEF_ROUTE=az_subnet_gateway,
        ROUTE_DESTINATION=route_destination
    )

def launch_instance_in_az(instance_info, target_az, security_group, keypair, user_data_template, route_destination):
    """Launch new instance in different AZ"""
    ec2 = boto3.client('ec2')

    # Get subnet in target AZ
    target_subnet = get_subnet_in_az(target_az, instance_info['subnet_id'])
    if not target_subnet:
        raise Exception(f"No subnet found in AZ {target_az}")

    # Get subnet gateway
    az_subnet_gateway = get_subnet_gateway(target_subnet)

    # Render user data template
    user_data = render_user_data(user_data_template, az_subnet_gateway, route_destination)

    # Generate random 5-digit ID and create instance name
    random_id = random.randint(10000, 99999)
    instance_name = f"sec-ip-vip-{random_id}"

    # Launch new instance
    response = ec2.run_instances(
        ImageId=instance_info['ami_id'],
        MinCount=1,
        MaxCount=1,
        InstanceType=instance_info['instance_type'],
        KeyName=keypair,
        SecurityGroupIds=[security_group],
        SubnetId=target_subnet,
        UserData=user_data,
        TagSpecifications=[
            {
                'ResourceType': 'instance',
                'Tags': [
                    {
                        'Key': 'Name',
                        'Value': instance_name
                    }
                ]
            }
        ]
    )

    new_instance_id = response['Instances'][0]['InstanceId']

    # Wait for running state
    waiter = ec2.get_waiter('instance_running')
    waiter.wait(InstanceIds=[new_instance_id])

    # Get ENI ID and disable source/dest check
    instance_info = ec2.describe_instances(InstanceIds=[new_instance_id])
    eni_id = instance_info['Reservations'][0]['Instances'][0]['NetworkInterfaces'][0]['NetworkInterfaceId']

    ec2.modify_network_interface_attribute(
        NetworkInterfaceId=eni_id,
        SourceDestCheck={'Value': False}
    )

    return new_instance_id

def main():
    parser = argparse.ArgumentParser(description='EC2 Controller - Monitor and failover instances')
    parser.add_argument('--instance-id', required=True, help='Controlled instance ID')
    parser.add_argument('--security-group', required=True, help='Security group ID for new instance')
    parser.add_argument('--keypair', required=True, help='Key pair name for new instance')
    parser.add_argument('--user-data-file', required=True, help='Path to Jinja2 user data template file')
    parser.add_argument('--route-destination', required=True, help='Destination CIDR for static route (e.g., 10.0.0.0/16)')

    args = parser.parse_args()

    try:
        current_instance_id = args.instance_id

        print(f"Starting continuous monitoring of instance {current_instance_id}")
        print("Press Ctrl+C to stop")

        while True:
            print(f"DEBUG: Starting monitoring cycle...")
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"DEBUG: Timestamp: {timestamp}")

            # Get instance info
            print(f"DEBUG: Getting instance info...")
            info = get_instance_info(current_instance_id)
            print(f"DEBUG: Instance info result: {info}")

            # Check if instance needs replacement
            print(f"DEBUG: Checking replacement logic...")
            needs_replacement = False
            reason = ""
            ping_reachable = False

            if not info:
                needs_replacement = True
                reason = "not found"
                print(f"DEBUG: Instance not found")
            elif info['state'] != 'running':
                needs_replacement = True
                reason = f"state is {info['state']}"
                print(f"DEBUG: Instance state: {info['state']}")
            else:
                print(f"DEBUG: Instance running, checking ping...")
                # Extract IP from route destination CIDR (e.g., 10.0.0.10/32 -> 10.0.0.10)
                route_ip = args.route_destination.split('/')[0]
                ping_reachable = ping_instance(route_ip)
                if not ping_reachable:
                    needs_replacement = True
                    reason = "unreachable"
                print(f"DEBUG: Ping result: {ping_reachable}")

            print(f"DEBUG: Needs replacement: {needs_replacement}, Reason: {reason}")

            # Always print current status
            if not info:
                print(f"[{timestamp}] Instance {current_instance_id} - NOT FOUND")
            elif info['state'] != 'running':
                print(f"[{timestamp}] Instance {current_instance_id} - STATE: {info['state']}")
            else:
                ping_status = "REACHABLE" if ping_reachable else "UNREACHABLE"
                print(f"[{timestamp}] Instance {current_instance_id} ({info['ip']}) in AZ {info['az']} - RUNNING, PING: {ping_status}")

            print(f"DEBUG: About to check replacement logic...")
            if needs_replacement:
                print(f"[{timestamp}] Launching replacement due to: {reason}")

                # Use last known info or get from any available AZ
                if not info:
                    # If no info available, use first available AZ
                    ec2 = boto3.client('ec2')
                    azs = ec2.describe_availability_zones()['AvailabilityZones']
                    target_az = azs[0]['ZoneName']
                    # Get any subnet in that AZ for VPC discovery
                    subnets = ec2.describe_subnets(Filters=[{'Name': 'availability-zone', 'Values': [target_az]}])
                    if not subnets['Subnets']:
                        print("No subnets available, retrying in 5 seconds...")
                        time.sleep(5)
                        continue
                    # Create minimal info for launch
                    info = {
                        'az': target_az,
                        'subnet_id': subnets['Subnets'][0]['SubnetId'],
                        'ami_id': 'ami-0c02fb55956c7d316',  # Default Amazon Linux 2
                        'instance_type': 't3.micro'
                    }

                other_azs = get_other_azs(info['az'])
                if not other_azs:
                    print("No other AZs available, retrying in 5 seconds...")
                    time.sleep(5)
                    continue

                target_az = other_azs[0]
                print(f"Launching new instance in AZ: {target_az}")

                new_instance_id = launch_instance_in_az(info, target_az, args.security_group, args.keypair, args.user_data_file, args.route_destination)
                print(f"New instance {new_instance_id} launched")

                # Wait for status checks to pass
                print("Waiting for instance status checks to pass...")
                ec2 = boto3.client('ec2')
                waiter = ec2.get_waiter('instance_status_ok')
                waiter.wait(InstanceIds=[new_instance_id])
                print("Instance status checks passed")

                # Wait for user data script to complete (configure secondary IP)
                print("Waiting 30 seconds for user data script to configure secondary IP...")
                time.sleep(30)

                # Update routes
                print("Updating static routes...")
                update_routes(current_instance_id, new_instance_id, args.route_destination)

                # Terminate old instance if it exists
                try:
                    ec2 = boto3.client('ec2')
                    ec2.terminate_instances(InstanceIds=[current_instance_id])
                    print(f"Old instance {current_instance_id} terminated")
                except:
                    print(f"Could not terminate old instance {current_instance_id}")

                current_instance_id = new_instance_id
                print(f"Now monitoring instance {current_instance_id}")

            print(f"DEBUG: Sleeping for 5 seconds...")
            time.sleep(5)

    except KeyboardInterrupt:
        print("\nMonitoring stopped by user")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
