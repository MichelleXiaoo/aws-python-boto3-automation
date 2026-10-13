# import statements
import boto3

# create ec2 client and instance name
ec2 = boto3.client('ec2', region_name = 'ap-southeast-2')
instance_name = 'boto3-test'

# store instance id
instance_id = None

# check if instance which you are trying to create already exists and only work with an instance that hasn't been terminated
instance_exists = False
existing_id = None

response = ec2.describe_instances(
    Filters=[{'Name': 'instance-state-name',
              'Values': ['pending', 'running', 'stopping', 'stopped']}]  # excluded "terminated" and "shutting-down"
)

for reservation in response['Reservations']:
    for instance in reservation['Instances']:
        for tag in instance.get('Tags', []):
            if tag['Key'] =='Name' and tag['Value'] == instance_name:
                instance_exists = True
                instance_id = instance['InstanceId']
                print(f"An instance named '{instance_name}' with id '{instance_id}' already exists.")
                break
        if instance_exists:
            break
    if instance_exists:
        break

if not instance_exists:
    # Launch a new EC2 instance if it hasn't already been created
    new_instance = ec2.run_instances(
        ImageId = 'ami-0cf4f199b81de70a9',
        MinCount = 1,
        MaxCount = 1,
        InstanceType = 't2.micro',
        KeyName = 'sydneyAZkey',
        TagSpecifications = [
            {
                'ResourceType': 'instance',
                'Tags': [
                    {
                        'Key': 'Name',
                        'Value': instance_name
                    },
                ]
            },
        ]
    )
    instance_id = new_instance['Instances'][0]['InstanceId']
    print(f"Successfully launched EC2 Instance {instance_name} with id {instance_id}.")

# # Stop an instance 
# ec2.stop_instances(InstanceIds=[instance_id])
# print(f"Instance {instance_name} with id {instance_id} has stopped")

# # Start an instance 
# ec2.start_instances(InstanceIds=[instance_id])
# print(f"Instance '{instance_name}' - {instance_id} started")

# Terminate an instance 
ec2.terminate_instances(InstanceIds=[instance_id])
print(f"Instance '{instance_name}' - {instance_id} terminated")
