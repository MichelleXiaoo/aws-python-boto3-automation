import boto3
from pathlib import Path
from botocore.exceptions import ClientError


# Instantiate a boto3 resource for S3 and print out the Buckets list:
s3 = boto3.resource('s3')
for bucket in s3.buckets.all():
    print(bucket.name)

#  Give the bucket a uique name:
bucket_name = 'mich-dct-crud-1'

# Check of bucket exists
# Create the bucket if it does ot exist
all_my_buckets = [bucket.name for bucket in s3.buckets.all()]
if bucket_name not in all_my_buckets:
    print(f"'{bucket_name}' bucket does not exist. Creating now...")
    s3.create_bucket(Bucket=bucket_name)
    print(f"'{bucket_name}' bucket has been created.")
else:
    print(f"'{bucket_name}' bucket already exists. No need to create a new one.")

# Create the first file "file_1.txt" and upload it to the bucket
file_path = Path(__file__).parent / 'file_1.txt' 
with open(file_path, "w", encoding="utf-8") as f:
    f.write("Hello S3! This greeting is from Melbourne.")

s3.Bucket(bucket_name).upload_file(Filename=str(file_path), Key="file_1.txt")

# Read and print the file content from the bucket
obj = s3.Object(bucket_name, "file_1.txt")
body = obj.get()['Body'].read()
print(body)

# Update 'file_1' in the bucket with new content from 'file_2'
file_path = Path(__file__).parent / 'file_2.txt' 
with open(file_path, "w", encoding="utf-8") as f:
    f.write("Hello S3! This is the 2nd greeting.")

s3.Object(bucket_name,"file_1.txt").put(Body = open("file_2.txt", 'rb'))
obj = s3.Object(bucket_name, "file_1.txt")
body = obj.get()['Body'].read()
print(body)

# # Delete the file from the bucket
# s3.Object(bucket_name, "file_1.txt").delete()

# # Delete the bucket (the bucket should be empty)
# bucket = s3.Bucket(bucket_name)
# bucket.delete()

# # Check the bucket list in the S3 again to confirm the bucket has been deleted:
# for bucket in s3.buckets.all():
#     print(bucket.name)



