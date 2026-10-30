# demo shop: v1 of the stack
resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"
}
resource "aws_subnet" "app" {
  vpc_id     = aws_vpc.main.id
  cidr_block = "10.0.1.0/24"
}
resource "aws_subnet" "data" {
  vpc_id     = aws_vpc.main.id
  cidr_block = "10.0.2.0/24"
}
resource "aws_security_group" "app" {
  vpc_id = aws_vpc.main.id
}
resource "aws_instance" "web" {
  subnet_id              = aws_subnet.app.id
  vpc_security_group_ids = [aws_security_group.app.id]
  instance_type          = "t3.micro"
}
resource "aws_db_instance" "orders" {
  engine                 = "postgres"
  instance_class         = "db.t3.micro"
  vpc_security_group_ids = [aws_security_group.app.id]
}
# v2 adds async order events
resource "aws_sqs_queue" "order_events" {}
resource "aws_lambda_function" "fulfil" {
  runtime = "python3.12"
  handler = "app.handler"
}
resource "aws_lambda_event_source_mapping" "orders" {
  event_source_arn = aws_sqs_queue.order_events.arn
  function_name    = aws_lambda_function.fulfil.arn
}
