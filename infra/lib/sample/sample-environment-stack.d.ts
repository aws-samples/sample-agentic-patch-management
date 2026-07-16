import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Construct } from 'constructs';
interface SampleEnvironmentStackProps extends cdk.StackProps {
    /** VPC to deploy instances into. If not provided, a minimal VPC is created
     *  (useful for spoke-account deployments where Patchy-Network doesn't exist). */
    vpc?: ec2.IVpc;
}
export declare class SampleEnvironmentStack extends cdk.Stack {
    readonly instances: ec2.Instance[];
    constructor(scope: Construct, id: string, props: SampleEnvironmentStackProps);
}
export {};
