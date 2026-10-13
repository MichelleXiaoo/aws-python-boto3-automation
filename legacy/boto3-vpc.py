import boto3
import time

ec2 = boto3.client('ec2', region_name = 'ap-southeast-2')

# 1. Create VPC
vpc_name = 'vpc-boto3'
response = ec2.describe_vpcs(
    Filters=[{'Name': 'tag:Name', 'Values':[vpc_name]}]
)
vpcs = response.get('Vpcs', [])

if vpcs:
    vpc_id = vpcs[0]['VpcId']
    print(f"VPC '{vpc_name}' with ID '{vpc_id}' already exists.")
else: 
    vpc_response = ec2.create_vpc(CidrBlock = '10.0.0.0/16')
    vpc_id = vpc_response['Vpc']['VpcId']

    time.sleep(5)

    ec2.create_tags(Resources = [vpc_id], Tags = [{'Key': 'Name', 'Value': vpc_name}])
    print(f"VPC named '{vpc_name}' with ID {vpc_id} has been created.")


# 2. Create internet gateway
ig_name = 'ig-vpc-boto3'
response = ec2.describe_internet_gateways(
    Filters=[{'Name': 'tag:Name', 'Values':[ig_name]}]
)
internet_gateways = response.get('InternetGateways', [])


if internet_gateways:
    ig = internet_gateways[0]
    ig_id = ig['InternetGatewayId']
    attached_vpcs = [a['VpcId'] for a in ig.get('Attachments',[])]
    print(f"Internet Gateway '{ig_name}' with ID '{ig_id}' already exists, attached to {attached_vpcs}.")
    if vpc_id not in attached_vpcs:                       # Not attached to the current VPC yet, so attach it
        for other_vpc in attached_vpcs:                   # If attached to another VPC, detach it first
            ec2.detach_internet_gateway(VpcId=other_vpc, InternetGatewayId=ig_id)
        ec2.attach_internet_gateway(VpcId=vpc_id, InternetGatewayId=ig_id)
        print(f"IGW {ig_id} has been attached to current VPC {vpc_id}.")
else: 
    ig_response = ec2.create_internet_gateway()
    ig_id = ig_response['InternetGateway']['InternetGatewayId']
    ec2.create_tags(Resources = [ig_id], Tags = [{'Key': 'Name', 'Value': ig_name}])
    ec2.attach_internet_gateway(VpcId=vpc_id, InternetGatewayId=ig_id)
    print(f"Internet Gateway named '{ig_name}' with ID {ig_id} has been created to VPC {vpc_id}.")

# 3. Create a route table and a public route
# # 3.1 Simple version: just CREATE the route table (TB) directly
# rt_response = ec2.create_route_table(VpcId=vpc_id) #Every successful run creates a new route table! Because no tag is applied here, some may be left behind when deleting via a function later
# rt_id = rt_response['RouteTable']['RouteTableId']
# route = ec2.create_route(
#     RouteTableId=rt_id,
#     DestinationCidrBlock='0.0.0.0/0',
#     GatewayId=ig_id
# )
# print(f"Route Table with ID '{rt_id}' has been created.")

# 3.2. The slightly more complex version checks whether the RT exists first; only create it if it doesn't exist, and if it exists, just PRINT it out
rt_name = 'rt-vpc-boto3'
response = ec2.describe_route_tables(
    Filters=[
        {'Name': 'vpc-id', 'Values': [vpc_id]},
        {'Name': 'tag:Name', 'Values': [rt_name]},
    ]
)
if response['RouteTables']:
    rt_id = response['RouteTables'][0]['RouteTableId']
    print(f"Route Table '{rt_name}' with ID '{rt_id}' already exists.")
else:
    rt_response = ec2.create_route_table(VpcId=vpc_id)
    rt_id = rt_response['RouteTable']['RouteTableId']
    ec2.create_route(
        RouteTableId=rt_id,
        DestinationCidrBlock='0.0.0.0/0',
        GatewayId=ig_id
    )
    ec2.create_tags(
        Resources=[rt_id],
        Tags=[{'Key': 'Name', 'Value': rt_name}]
    )
    print(f"Route Table '{rt_name}' with ID '{rt_id}' has been created.")

# 4. Create 3 subnets
# # 4.1 The simple version is also to directly CREATE:
# subnet_1 = ec2.create_subnet(VpcId=vpc_id, CidrBlock='10.0.1.0/24', AvailabilityZone='ap-southeast-2a')
# subnet_2 = ec2.create_subnet(VpcId=vpc_id, CidrBlock='10.0.2.0/24', AvailabilityZone='ap-southeast-2b')
# subnet_3 = ec2.create_subnet(VpcId=vpc_id, CidrBlock='10.0.3.0/24', AvailabilityZone='ap-southeast-2c')
# print(f"Subnet_1 ID = '{subnet_1['Subnet']['SubnetId']}', Subnet_2 ID = '{subnet_2['Subnet']['SubnetId']}', Subnet_3 ID = '{subnet_3['Subnet']['SubnetId']}' have been created.")

# 4.2 The complex version also needs to check existence beforehand, only creating it if it doesn't exist; since 3 need to be created, making it a function is more convenient
def ensure_subnet(cidr, az):
    response = ec2.describe_subnets(
        Filters=[
            {'Name': 'vpc-id', 'Values': [vpc_id]},
            {'Name': 'cidr-block', 'Values': [cidr]},
        ]
    )
    if response['Subnets']:
        subnet_id = response['Subnets'][0]['SubnetId']
        print(f"Subnet {cidr} with ID {subnet_id} already exists.")
    else:
        response = ec2.create_subnet(VpcId=vpc_id, CidrBlock=cidr, AvailabilityZone=az)
        subnet_id = response['Subnet']['SubnetId']
        print(f"Created subnet {cidr}: {subnet_id}")
    return subnet_id

subnet_1_id = ensure_subnet('10.0.1.0/24', 'ap-southeast-2a')
subnet_2_id = ensure_subnet('10.0.2.0/24', 'ap-southeast-2b')
subnet_3_id = ensure_subnet('10.0.3.0/24', 'ap-southeast-2c')

# 5. Associate RT to subnet
# # 5.1 Direct version:
# for subnet_id in [subnet_1_id, subnet_2_id, subnet_3_id]:
#     ec2.associate_route_table(RouteTableId=rt_id, SubnetId=subnet_id)
#     print(f"Associated {subnet_id} with route table {rt_id}")

# 5.2 Using a function
def ensure_association(subnet_id):
    rt = ec2.describe_route_tables(RouteTableIds=[rt_id])['RouteTables'][0]
    associated_subnets = [association.get('SubnetId') for association in rt['Associations']]
    if subnet_id in associated_subnets:
        print(f"Subnet {subnet_id} has associated with route table {rt_id}.")
    else:
        ec2.associate_route_table(RouteTableId=rt_id, SubnetId=subnet_id)
        print(f"Associated {subnet_id} with route table {rt_id}.")     

for subnet_id in [subnet_1_id, subnet_2_id, subnet_3_id]:
    ensure_association(subnet_id)


