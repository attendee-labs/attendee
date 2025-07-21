# ----------------------------------------
# ❶ VPC
# ----------------------------------------
resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = {
    Name = "eks-rds-vpc"
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs          = slice(data.aws_availability_zones.available.names, 0, 3)
  public_cidrs = cidrsubnets("10.0.0.0/16", 4, 4, 4)    # 10.0.0.0/20, 10.0.16.0/20, 10.0.32.0/20
  app_cidrs    = cidrsubnets("10.0.64.0/18", 4, 4, 4)   # 10.0.64.0/20, 10.0.80.0/20, 10.0.96.0/20
  db_cidrs     = cidrsubnets("10.0.128.0/18", 4, 4, 4)  # 10.0.128.0/20, 10.0.144.0/20, 10.0.160.0/20
}

# ----------------------------------------
# ❷ Subnets
# ----------------------------------------
resource "aws_subnet" "public" {
  for_each                = toset(local.azs)
  vpc_id                  = aws_vpc.main.id
  cidr_block              = local.public_cidrs[index(local.azs, each.key)]
  availability_zone       = each.key
  map_public_ip_on_launch = true
  tags = {
    Name                         = "public-${each.key}"
    "kubernetes.io/role/elb"     = "1"
    "kubernetes.io/cluster/demo" = "shared"
  }
}

resource "aws_subnet" "app" {
  for_each          = toset(local.azs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.app_cidrs[index(local.azs, each.key)]
  availability_zone = each.key
  tags = {
    Name                              = "app-${each.key}"
    "kubernetes.io/role/internal-elb" = "1"
    "kubernetes.io/cluster/demo"      = "shared"
  }
}

resource "aws_subnet" "db" {
  for_each          = toset(local.azs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.db_cidrs[index(local.azs, each.key)]
  availability_zone = each.key
  tags = {
    Name = "db-${each.key}"
  }
}

# ----------------------------------------
# ❸ Internet gateway + public route table
# ----------------------------------------
resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "main-igw" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }
  
  tags = { Name = "public-rt" }
}

resource "aws_route_table_association" "public" {
  for_each       = aws_subnet.public
  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

# ----------------------------------------
# ❹ NAT Gateways + private‑app route tables
# ----------------------------------------
resource "aws_eip" "nat" {
  for_each = aws_subnet.public
  domain   = "vpc"
  tags     = { Name = "nat-eip-${each.key}" }
}

resource "aws_nat_gateway" "nat" {
  for_each      = aws_subnet.public
  subnet_id     = each.value.id
  allocation_id = aws_eip.nat[each.key].id
  tags          = { Name = "nat-${each.key}" }
}

resource "aws_route_table" "app" {
  for_each = aws_nat_gateway.nat
  vpc_id   = aws_vpc.main.id
  
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = each.value.id
  }
  
  tags = { Name = "app-rt-${each.key}" }
}

resource "aws_route_table_association" "app" {
  for_each       = aws_subnet.app
  subnet_id      = each.value.id
  route_table_id = aws_route_table.app[each.key].id
}

# ----------------------------------------
# ❺ Isolated DB route tables (no IGW / NAT)
# ----------------------------------------
resource "aws_route_table" "db" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "db-rt" }
}

resource "aws_route_table_association" "db" {
  for_each       = aws_subnet.db
  subnet_id      = each.value.id
  route_table_id = aws_route_table.db.id
}

# ----------------------------------------
# ❻ RDS Subnet Group
# ----------------------------------------
resource "aws_db_subnet_group" "this" {
  name       = "rds-db-subnet-group"
  subnet_ids = [for s in aws_subnet.db : s.id]
  tags       = { Name = "rds-private" }
}
