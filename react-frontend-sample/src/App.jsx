import { useEffect, useMemo, useRef, useState } from 'react';
import { motion } from 'framer-motion';
import Chatbot from './components/Chatbot';

const debounceDelay = 500;

const useArtifactCleanup = (artifacts) => {
  useEffect(() => {
    return () => {
      artifacts.forEach((item) => URL.revokeObjectURL(item.url));
    };
  }, [artifacts]);
};

const createDownloadEntries = (artifacts = []) => {
  return artifacts.map((artifact, idx) => {
    const binary = atob(artifact.data);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    const blob = new Blob([bytes], { type: artifact.content_type || 'application/octet-stream' });
    const url = URL.createObjectURL(blob);
    return {
      url,
      filename: artifact.filename || `artifact-${idx}`,
    };
  });
};

async function postJson(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const payload = await res.json();
  if (!res.ok) {
    const error = new Error(payload.error || 'Request failed');
    if (payload.logs) {
      error.logs = payload.logs;
    }
    if (payload.details) {
      error.details = payload.details;
    }
    throw error;
  }
  return payload;
}

async function runStreamingTask(url, body, onLog) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let payload = null;
    try {
      payload = await res.json();
    } catch (err) {
      /* ignore */
    }
    const message = payload?.error || `Request failed with status ${res.status}`;
    throw new Error(message);
  }
  if (!res.body) {
    throw new Error('Streaming not supported in this browser.');
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    let newlineIndex;
    // Process newline-delimited JSON events
    while ((newlineIndex = buffer.indexOf('\n')) >= 0) {
      const chunk = buffer.slice(0, newlineIndex).trim();
      buffer = buffer.slice(newlineIndex + 1);
      if (!chunk) {
        continue;
      }
      let event;
      try {
        event = JSON.parse(chunk);
      } catch (err) {
        continue;
      }
      if (event.event === 'log') {
        if (onLog && event.message) {
          onLog(event.message);
        }
      } else if (event.event === 'result') {
        if (event.status !== 'ok') {
          throw new Error(event.error || 'Task failed.');
        }
        return event;
      }
    }
  }
  throw new Error('Stream ended unexpectedly.');
}

const AWS_REGIONS = [
  { id: 'us-east-1', label: 'US East (N. Virginia)' },
  { id: 'us-east-2', label: 'US East (Ohio)' },
  { id: 'us-west-1', label: 'US West (N. California)' },
  { id: 'us-west-2', label: 'US West (Oregon)' },
  { id: 'ap-south-1', label: 'Asia Pacific (Mumbai)' },
  { id: 'ap-northeast-1', label: 'Asia Pacific (Tokyo)' },
  { id: 'ap-northeast-2', label: 'Asia Pacific (Seoul)' },
  { id: 'ap-southeast-1', label: 'Asia Pacific (Singapore)' },
  { id: 'ap-southeast-2', label: 'Asia Pacific (Sydney)' },
  { id: 'eu-central-1', label: 'EU (Frankfurt)' },
  { id: 'eu-west-1', label: 'EU (Ireland)' },
  { id: 'eu-west-2', label: 'EU (London)' },
  { id: 'eu-west-3', label: 'EU (Paris)' },
  { id: 'sa-east-1', label: 'South America (São Paulo)' },
  { id: 'me-south-1', label: 'Middle East (Bahrain)' },
  { id: 'af-south-1', label: 'Africa (Cape Town)' },
  { id: 'ap-east-1', label: 'Asia Pacific (Hong Kong)' },
  { id: 'custom', label: 'Custom region...' },
];
const formatLocationSuffix = (rawLabel) => {
  if (!rawLabel) return '';
  const match = rawLabel.match(/\(([^)]+)\)/);
  const target = match ? match[1] : rawLabel;
  const cleaned = target
    .replace(/\s+/g, ' ')
    .trim();
  const tokens = cleaned.split(/[\s/-]+/).filter(Boolean);
  if (tokens.length > 1) {
    const abbrev = tokens
      .map((token) => token.replace(/[^A-Za-z0-9]/g, '').charAt(0))
      .filter(Boolean)
      .map((ch) => ch.toUpperCase())
      .join('.');
    return abbrev || cleaned.replace(/\s+/g, '').toUpperCase();
  }
  return cleaned.replace(/\s+/g, '').toUpperCase();
};

const formatRegionDisplay = (id, label) => {
  const suffix = formatLocationSuffix(label);
  return suffix ? `${id.toUpperCase()}(${suffix})` : id.toUpperCase();
};

const AWS_REGION_CHOICES = AWS_REGIONS.filter((region) => region.id !== 'custom');
const AWS_TO_GCP_REGION = {
  'us-east-1': 'us-east1',
  'us-east-2': 'us-east4',
  'us-west-1': 'us-west2',
  'us-west-2': 'us-west1',
  'ca-central-1': 'northamerica-northeast1',
  'eu-west-1': 'europe-west1',
  'eu-west-2': 'europe-west2',
  'eu-west-3': 'europe-west9',
  'eu-central-1': 'europe-west3',
  'eu-north-1': 'europe-north1',
  'eu-south-1': 'europe-southwest1',
  'ap-south-1': 'asia-south1',
  'ap-south-2': 'asia-south2',
  'ap-southeast-1': 'asia-southeast1',
  'ap-southeast-2': 'australia-southeast1',
  'ap-southeast-3': 'asia-southeast2',
  'ap-northeast-1': 'asia-northeast1',
  'ap-northeast-2': 'asia-northeast3',
  'ap-northeast-3': 'asia-northeast2',
  'ap-east-1': 'asia-east2',
  'sa-east-1': 'southamerica-east1',
  'me-south-1': 'me-central1',
};
const GCP_REGION_LABELS = {
  'us-central1': 'US Central (Iowa)',
  'us-east1': 'US East (South Carolina)',
  'us-east4': 'US East (Northern Virginia)',
  'us-west1': 'US West (Oregon)',
  'us-west2': 'US West (Los Angeles)',
  'northamerica-northeast1': 'Canada (Montreal)',
  'europe-west1': 'Europe West 1 (Belgium)',
  'europe-west2': 'Europe West 2 (London)',
  'europe-west3': 'Europe West 3 (Frankfurt)',
  'europe-west9': 'Europe West 9 (Paris)',
  'europe-north1': 'Europe North (Finland)',
  'europe-southwest1': 'Europe Southwest (Madrid)',
  'asia-south1': 'Asia South 1 (Mumbai)',
  'asia-south2': 'Asia South 2 (Delhi)',
  'asia-southeast1': 'Asia Southeast 1 (Singapore)',
  'asia-southeast2': 'Asia Southeast 2 (Jakarta)',
  'australia-southeast1': 'Australia Southeast 1 (Sydney)',
  'asia-northeast1': 'Asia Northeast 1 (Tokyo)',
  'asia-northeast2': 'Asia Northeast 2 (Osaka)',
  'asia-northeast3': 'Asia Northeast 3 (Seoul)',
  'asia-east2': 'Asia East 2 (Hong Kong)',
  'southamerica-east1': 'South America East (São Paulo)',
  'me-central1': 'Middle East (Doha)',
};
const GCP_REGIONS = Object.entries(GCP_REGION_LABELS).map(([id, label]) => ({
  id,
  label,
  display: formatRegionDisplay(id, label),
}));
const DEFAULT_EKS_RESOURCE_TYPES = [
  'deployments.apps',
  'statefulsets.apps',
  'daemonsets.apps',
  'jobs.batch',
  'cronjobs.batch',
  'services',
  'ingresses.networking.k8s.io',
  'configmaps',
  'secrets',
  'persistentvolumeclaims',
  'horizontalpodautoscalers.autoscaling',
];
const INVENTORY_RESOURCE_IDS = [
  'cost',
  'rds',
  'elasticache',
  'backup',
  'secretsmanager',
  'appsync',
  'dynamodb',
  'cloudwatch',
  'ecs',
  'kms',
  'mq',
  'codecommit',
  'codepipeline',
  'ecr',
  'codebuild',
  'codeartifact',
  'cloudformation',
  'waf',
  'eks',
  'codedeploy',
  'vpc',
  'iam_identity',
  'ec2',
  'redshift',
  'sqs',
  'stepfunctions',
  'route53',
  'sns',
  'ebs',
  'glue',
  'efs',
  'amplify',
  'cloudfront',
  's3',
  'iam_user',
  'iam_group',
  'iam_policies',
  'iam_role',
];

const prettyLabel = (value) =>
  value
    .split('_')
    .map((chunk) => chunk.charAt(0).toUpperCase() + chunk.slice(1))
    .join(' ');

const INVENTORY_RESOURCE_CHOICES = INVENTORY_RESOURCE_IDS.map((id) => ({
  id,
  label: prettyLabel(id),
}));
const regionLabel = (regionId) => {
  const region = AWS_REGIONS.find((entry) => entry.id === regionId);
  if (!region) {
    return regionId.toUpperCase();
  }
  return formatRegionDisplay(region.id, region.label);
};

const getAwsRegionDisplay = (region) =>
  region.id === 'custom' ? region.label : formatRegionDisplay(region.id, region.label);

const getGcpRegionDisplay = (regionId) => {
  const label = GCP_REGION_LABELS[regionId];
  return label ? formatRegionDisplay(regionId, label) : regionId.toUpperCase();
};

const filterSubnetsByRegion = (subnets, region) => {
  if (!region) return subnets;
  return (subnets || []).filter((subnet) => (subnet.region || '').toLowerCase() === region.toLowerCase());
};

const Dashboard = () => {
  const [darkMode, setDarkMode] = useState(() => {
    const saved = localStorage.getItem('darkMode');
    return saved === 'true';
  });
  
  const [awsAccess, setAwsAccess] = useState('');
  const [awsSecret, setAwsSecret] = useState('');
  const [awsRegion, setAwsRegion] = useState(AWS_REGIONS[0].id);
  
  // Dark mode effect
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', darkMode ? 'dark' : 'light');
    localStorage.setItem('darkMode', darkMode);
  }, [darkMode]);
  
  const [customRegion, setCustomRegion] = useState('');
  const [vpcs, setVpcs] = useState([]);
  const [selectedVpc, setSelectedVpc] = useState('');
  const [subnets, setSubnets] = useState([]);
  const [detectedAwsAsn, setDetectedAwsAsn] = useState(null);
  const [attachedVgw, setAttachedVgw] = useState(null);
  const [vpcError, setVpcError] = useState('');
  const [subnetError, setSubnetError] = useState('');

  const [gcpProject, setGcpProject] = useState('');
  const [gcpNetwork, setGcpNetwork] = useState('');
  const [gcpRegion, setGcpRegion] = useState('us-central1');

  const [tfLogs, setTfLogs] = useState('');
  const [tfError, setTfError] = useState('');
  const [tfArtifacts, setTfArtifacts] = useState([]);

  const [invRegions, setInvRegions] = useState([AWS_REGION_CHOICES[0]?.id || 'us-east-1']);
  const [invResources, setInvResources] = useState(['ec2']);
  const [invFrom, setInvFrom] = useState('');
  const [invTo, setInvTo] = useState('');
  const [invLogs, setInvLogs] = useState('');
  const [invError, setInvError] = useState('');
  const [invArtifacts, setInvArtifacts] = useState([]);
  const [invLoading, setInvLoading] = useState(false);
  const [invStatus, setInvStatus] = useState('');
  const [auditLogs, setAuditLogs] = useState('');
  const [auditError, setAuditError] = useState('');
  const [auditArtifacts, setAuditArtifacts] = useState([]);
  const [auditLoading, setAuditLoading] = useState(false);
  const [gcpAuditServiceKey, setGcpAuditServiceKey] = useState('');
  const [gcpAuditServiceFileName, setGcpAuditServiceFileName] = useState('');
  const [gcpAuditProjects, setGcpAuditProjects] = useState([]);
  const [selectedGcpAuditProjects, setSelectedGcpAuditProjects] = useState([]);
  const [gcpAuditProjectError, setGcpAuditProjectError] = useState('');
  const [gcpAuditProjectsLoading, setGcpAuditProjectsLoading] = useState(false);
  const [gcpAuditLogs, setGcpAuditLogs] = useState('');
  const [gcpAuditError, setGcpAuditError] = useState('');
  const [gcpAuditArtifacts, setGcpAuditArtifacts] = useState([]);
  const [gcpAuditLoading, setGcpAuditLoading] = useState(false);
  const [tcoCsvContent, setTcoCsvContent] = useState('');
  const [tcoFileName, setTcoFileName] = useState('');
  const [tcoLogs, setTcoLogs] = useState('');
  const [tcoError, setTcoError] = useState('');
  const [tcoArtifacts, setTcoArtifacts] = useState([]);
  const [tcoLoading, setTcoLoading] = useState(false);
  const invProgressTimer = useRef(null);
  const gcpSubnetCacheRef = useRef(new Map());
  const gcpSubnetRequestRef = useRef(0);
  const [view, setView] = useState('home');
  const [ecrGcpProject, setEcrGcpProject] = useState('');
  const [ecrGcpRegion, setEcrGcpRegion] = useState('us-central1');
  const [ecrWorkers, setEcrWorkers] = useState(4);
  const [ecrLogs, setEcrLogs] = useState('');
  const [ecrError, setEcrError] = useState('');
  const [ecrArtifacts, setEcrArtifacts] = useState([]);
  const [ecrRepos, setEcrRepos] = useState([]);
  const [selectedEcrRepos, setSelectedEcrRepos] = useState([]);
  const [ecrRepoError, setEcrRepoError] = useState('');
  const [ecrServiceKey, setEcrServiceKey] = useState('');
  const [ecrServiceFileName, setEcrServiceFileName] = useState('');
  const [ecrProjectOptions, setEcrProjectOptions] = useState([]);
  const [ecrProjectError, setEcrProjectError] = useState('');
  const ecrMaxWorkers = useMemo(() => Math.max(1, (navigator.hardwareConcurrency || 8) * 2), []);
  const [vpnServiceKey, setVpnServiceKey] = useState('');
  const [vpnGcpProject, setVpnGcpProject] = useState('');
  const [vpnGcpRegion, setVpnGcpRegion] = useState('us-central1');
  const [vpnGcpNetworks, setVpnGcpNetworks] = useState([]);
  const [vpnGcpNetwork, setVpnGcpNetwork] = useState('');
  const [vpnNetworkError, setVpnNetworkError] = useState('');
  const [vpnGcpSubnets, setVpnGcpSubnets] = useState([]);
  const [vpnSubnetError, setVpnSubnetError] = useState('');
  const [classicAwsAsn, setClassicAwsAsn] = useState('64513');
  const [classicGcpAsn, setClassicGcpAsn] = useState('64512');
  const [classicPrefix, setClassicPrefix] = useState('');
  const [classicIkeVersion, setClassicIkeVersion] = useState('1');
  const [classicLogs, setClassicLogs] = useState('');
  const [classicError, setClassicError] = useState('');
  const [classicArtifacts, setClassicArtifacts] = useState([]);
  const [classicAsnError, setClassicAsnError] = useState('');
  const [selectedAwsSubnets, setSelectedAwsSubnets] = useState([]);
  const [selectedGcpSubnets, setSelectedGcpSubnets] = useState([]);
  const [vpnServiceFileName, setVpnServiceFileName] = useState('');
  const [vpnProjectOptions, setVpnProjectOptions] = useState([]);
  const [vpnProjectError, setVpnProjectError] = useState('');
  const [vpnSubnetsLoading, setVpnSubnetsLoading] = useState(false);
  const [haAwsAsn, setHaAwsAsn] = useState('64513');
  const [haGcpAsn, setHaGcpAsn] = useState('64512');
  const [haPrefix, setHaPrefix] = useState('');
  const [haLogs, setHaLogs] = useState('');
  const [haError, setHaError] = useState('');
  const [haArtifacts, setHaArtifacts] = useState([]);
  const [haAsnError, setHaAsnError] = useState('');
  const [ecsClusters, setEcsClusters] = useState([]);
  const [ecsClusterLoading, setEcsClusterLoading] = useState(false);
  const [ecsClusterError, setEcsClusterError] = useState('');
  const [ecsClusterName, setEcsClusterName] = useState('');
  const [ecsServices, setEcsServices] = useState([]);
  const [ecsServicesLoading, setEcsServicesLoading] = useState(false);
  const [ecsServicesError, setEcsServicesError] = useState('');
  const [ecsTerraformServices, setEcsTerraformServices] = useState([]);
  const [ecsManifestServices, setEcsManifestServices] = useState([]);
  const [ecsTfGcpProject, setEcsTfGcpProject] = useState('');
  const [ecsTfGcpLocation, setEcsTfGcpLocation] = useState('us-central1');
  const [ecsTfGkeName, setEcsTfGkeName] = useState('');
  const [ecsTfMachineType, setEcsTfMachineType] = useState('e2-standard-4');
  const [ecsTfNodeCpu, setEcsTfNodeCpu] = useState('');
  const [ecsTfNodeMem, setEcsTfNodeMem] = useState('');
  const [ecsTfMinNodes, setEcsTfMinNodes] = useState('3');
  const [ecsTfMaxNodes, setEcsTfMaxNodes] = useState('6');
  const [ecsTfNodeLocations, setEcsTfNodeLocations] = useState('');
  const [ecsTfNetwork, setEcsTfNetwork] = useState('');
  const [ecsTfSubnetwork, setEcsTfSubnetwork] = useState('');
  const [ecsTfServiceAccount, setEcsTfServiceAccount] = useState('');
  const [ecsTfReleaseChannel, setEcsTfReleaseChannel] = useState('REGULAR');
  const [ecsTfMasterCidr, setEcsTfMasterCidr] = useState('');
  const [ecsTfNodePoolName, setEcsTfNodePoolName] = useState('primary');
  const [ecsTfNodePoolSubnet, setEcsTfNodePoolSubnet] = useState('');
  const [ecsTfNodePoolZones, setEcsTfNodePoolZones] = useState('');
  const [ecsTfPrivateNodes, setEcsTfPrivateNodes] = useState(true);
  const [ecsTfPrivateEndpoint, setEcsTfPrivateEndpoint] = useState(false);
  const [ecsTfLogs, setEcsTfLogs] = useState('');
  const [ecsTfError, setEcsTfError] = useState('');
  const [ecsTfArtifacts, setEcsTfArtifacts] = useState([]);
  const [ecsManifestLogs, setEcsManifestLogs] = useState('');
  const [ecsManifestError, setEcsManifestError] = useState('');
  const [ecsManifestArtifacts, setEcsManifestArtifacts] = useState([]);

  // DocumentDB to Atlas Migration state
  const [docdbAtlasUri, setDocdbAtlasUri] = useState('');
  const [docdbDocdbUri, setDocdbDocdbUri] = useState('');
  const [docdbMode, setDocdbMode] = useState('fresh');
  const [docdbAction, setDocdbAction] = useState('migrate');
  const [docdbDatabases, setDocdbDatabases] = useState('');
  const [docdbNumWorkers, setDocdbNumWorkers] = useState(8);
  const [docdbNumParallelCollections, setDocdbNumParallelCollections] = useState(4);
  const [docdbTimestampField, setDocdbTimestampField] = useState('auto');
  const [docdbMatchIndexNames, setDocdbMatchIndexNames] = useState(false);
  const [docdbDeleteLocalAfter, setDocdbDeleteLocalAfter] = useState(false);
  const [docdbDryRun, setDocdbDryRun] = useState(false);
  const [docdbInitSource, setDocdbInitSource] = useState('atlas');
  const [docdbLogs, setDocdbLogs] = useState('');
  const [docdbError, setDocdbError] = useState('');
  const [docdbArtifacts, setDocdbArtifacts] = useState([]);
  const [docdbLoading, setDocdbLoading] = useState(false);

  const [eksClusters, setEksClusters] = useState([]);
  const [eksClusterLoading, setEksClusterLoading] = useState(false);
  const [eksClusterError, setEksClusterError] = useState('');
  const [eksClusterName, setEksClusterName] = useState('');
  const [eksTfGcpProject, setEksTfGcpProject] = useState('');
  const [eksTfGcpLocation, setEksTfGcpLocation] = useState('us-central1');
  const [eksTfGkeName, setEksTfGkeName] = useState('');
  const [eksTfMachineType, setEksTfMachineType] = useState('e2-standard-4');
  const [eksTfNodeCpu, setEksTfNodeCpu] = useState('');
  const [eksTfNodeMem, setEksTfNodeMem] = useState('');
  const [eksTfMinNodes, setEksTfMinNodes] = useState('3');
  const [eksTfMaxNodes, setEksTfMaxNodes] = useState('6');
  const [eksTfNodeLocations, setEksTfNodeLocations] = useState('');
  const [eksTfNetwork, setEksTfNetwork] = useState('');
  const [eksTfSubnetwork, setEksTfSubnetwork] = useState('');
  const [eksTfServiceAccount, setEksTfServiceAccount] = useState('');
  const [eksTfReleaseChannel, setEksTfReleaseChannel] = useState('REGULAR');
  const [eksTfMasterCidr, setEksTfMasterCidr] = useState('');
  const [eksTfPrivateNodes, setEksTfPrivateNodes] = useState(true);
  const [eksTfPrivateEndpoint, setEksTfPrivateEndpoint] = useState(false);
  const [eksTfLogs, setEksTfLogs] = useState('');
  const [eksTfError, setEksTfError] = useState('');
  const [eksTfArtifacts, setEksTfArtifacts] = useState([]);
  const [eksNamespaces, setEksNamespaces] = useState([]);
  const [eksNamespacesLoading, setEksNamespacesLoading] = useState(false);
  const [eksNamespacesError, setEksNamespacesError] = useState('');
  const [selectedEksNamespaces, setSelectedEksNamespaces] = useState([]);
  const [selectedEksResourceTypes, setSelectedEksResourceTypes] = useState(DEFAULT_EKS_RESOURCE_TYPES.slice());
  const [eksManifestLogs, setEksManifestLogs] = useState('');
  const [eksManifestError, setEksManifestError] = useState('');
  const [eksManifestArtifacts, setEksManifestArtifacts] = useState([]);
  
  // VM2GKE state
  const [vm2gkeProvider, setVm2gkeProvider] = useState('aws');
  const [vm2gkeAwsRegion, setVm2gkeAwsRegion] = useState('');
  const [vm2gkeGcpProject, setVm2gkeGcpProject] = useState('');
  const [vm2gkeGcpRegion, setVm2gkeGcpRegion] = useState('');
  const [vm2gkeGcpServiceKey, setVm2gkeGcpServiceKey] = useState('');
  const [vm2gkeGcpServiceFileName, setVm2gkeGcpServiceFileName] = useState('');
  const [vm2gkeGcpProjectOptions, setVm2gkeGcpProjectOptions] = useState([]);
  const [vm2gkeGcpProjectError, setVm2gkeGcpProjectError] = useState('');
  const [vm2gkeGcpProjectsLoading, setVm2gkeGcpProjectsLoading] = useState(false);
  const [vm2gkeInstances, setVm2gkeInstances] = useState([]);
  const [vm2gkeInstancesLoading, setVm2gkeInstancesLoading] = useState(false);
  const [vm2gkeInstancesError, setVm2gkeInstancesError] = useState('');
  const [vm2gkeSelectedInstance, setVm2gkeSelectedInstance] = useState(null);
  const [vm2gkeDockerDiscoveryInitiated, setVm2gkeDockerDiscoveryInitiated] = useState(false);
  const [vm2gkeDockerContainers, setVm2gkeDockerContainers] = useState([]);
  const [vm2gkeSelectedContainers, setVm2gkeSelectedContainers] = useState([]);
  const [vm2gkeDockerImages, setVm2gkeDockerImages] = useState([]);
  const [vm2gkeDockerEnvVars, setVm2gkeDockerEnvVars] = useState({});
  const [vm2gkeDockerLoading, setVm2gkeDockerLoading] = useState(false);
  const [vm2gkeDockerError, setVm2gkeDockerError] = useState('');
  const [vm2gkeNamespace, setVm2gkeNamespace] = useState('');
  const [vm2gkeLogs, setVm2gkeLogs] = useState('');
  const [vm2gkeError, setVm2gkeError] = useState('');
  const [vm2gkeArtifacts, setVm2gkeArtifacts] = useState([]);
  
  // AWS Box Project with boto3 integration
  const [boxAwsRegions, setBoxAwsRegions] = useState([]);
  const [boxAwsRegionsLoading, setBoxAwsRegionsLoading] = useState(false);
  const [boxAwsRegionsError, setBoxAwsRegionsError] = useState('');
  const [boxAwsSelectedRegion, setBoxAwsSelectedRegion] = useState('us-east-1');
  const [boxAwsSelectedServices, setBoxAwsSelectedServices] = useState([]);
  const [boxAwsServiceConfigs, setBoxAwsServiceConfigs] = useState({});
  const [boxAwsEc2Data, setBoxAwsEc2Data] = useState({ amis: [], instance_types: [], instance_type_details: [] });
  const [boxAwsEc2DataLoading, setBoxAwsEc2DataLoading] = useState(false);
  const [boxAwsEc2AmiTab, setBoxAwsEc2AmiTab] = useState('quick-start'); // 'quick-start', 'my-amis', 'recents'
  const [boxAwsEc2InstanceName, setBoxAwsEc2InstanceName] = useState('');
  const [boxAwsEc2SelectedAmi, setBoxAwsEc2SelectedAmi] = useState(null);
  const [boxAwsEc2StorageSize, setBoxAwsEc2StorageSize] = useState(8);
  const [boxAwsEc2StorageType, setBoxAwsEc2StorageType] = useState('gp3');
  const [boxAwsEc2StorageIops, setBoxAwsEc2StorageIops] = useState(3000);
  const [boxAwsEc2StorageEncrypted, setBoxAwsEc2StorageEncrypted] = useState(false);
  const [boxAwsServiceExpanded, setBoxAwsServiceExpanded] = useState({
    vpc: true,
    ec2: true,
    s3: true,
    rds: true,
    ebs: true,
    efs: true
  });
  const [boxAwsEc2KeyPairName, setBoxAwsEc2KeyPairName] = useState('');
  const [boxAwsEc2KeyPairGenerating, setBoxAwsEc2KeyPairGenerating] = useState(false);
  const [boxAwsGeneratedKeyPair, setBoxAwsGeneratedKeyPair] = useState(null); // { key_name, private_key, public_key }
  const [boxAwsVpcCount, setBoxAwsVpcCount] = useState(1);
  const [boxAwsVpcExpanded, setBoxAwsVpcExpanded] = useState({}); // Track which VPCs are expanded
  const [boxAwsVpcTotalSubnets, setBoxAwsVpcTotalSubnets] = useState(2);
  const [boxAwsS3Count, setBoxAwsS3Count] = useState(1);
  const [boxAwsS3BucketsExpanded, setBoxAwsS3BucketsExpanded] = useState({});
  const [boxAwsRdsCount, setBoxAwsRdsCount] = useState(1);
  const [boxAwsRdsDatabasesExpanded, setBoxAwsRdsDatabasesExpanded] = useState({});
  const [boxAwsEfsCount, setBoxAwsEfsCount] = useState(1);
  const [boxAwsEfsFilesystemsExpanded, setBoxAwsEfsFilesystemsExpanded] = useState({});
  const [boxAwsRdsData, setBoxAwsRdsData] = useState({ engines: [], instance_classes: [] });
  const [boxAwsRdsDataLoading, setBoxAwsRdsDataLoading] = useState(false);
  const [boxAwsAvailabilityZones, setBoxAwsAvailabilityZones] = useState([]);
  const [boxAwsAvailabilityZonesLoading, setBoxAwsAvailabilityZonesLoading] = useState(false);
  const [boxAwsEc2OsType, setBoxAwsEc2OsType] = useState('amazon-linux');
  const [boxAwsEc2OsVersion, setBoxAwsEc2OsVersion] = useState('2023');
  // Multiple EC2 instances support
  const [boxAwsEc2Instances, setBoxAwsEc2Instances] = useState([
    { id: 1, name: 'web-server-1', expanded: true, ebsVolumes: [], keyPairSelection: 'select' }
  ]);
  const [boxAwsEc2InstancesExpanded, setBoxAwsEc2InstancesExpanded] = useState({ 1: true });
  // Key pair management - store created keys for reuse
  const [boxAwsEc2KeyPairList, setBoxAwsEc2KeyPairList] = useState([]);
  // Security group rules configuration
  const [boxAwsEc2SecurityGroupRules, setBoxAwsEc2SecurityGroupRules] = useState([
    { id: 1, port: 22, protocol: 'tcp', cidr: '0.0.0.0/0', description: 'SSH' },
    { id: 2, port: 80, protocol: 'tcp', cidr: '0.0.0.0/0', description: 'HTTP' },
    { id: 3, port: 443, protocol: 'tcp', cidr: '0.0.0.0/0', description: 'HTTPS' }
  ]);
  const [boxAwsArtifacts, setBoxAwsArtifacts] = useState([]);
  const [boxAwsLogs, setBoxAwsLogs] = useState('');
  const [boxAwsError, setBoxAwsError] = useState('');

  useArtifactCleanup(tfArtifacts);
  useArtifactCleanup(invArtifacts);
  useArtifactCleanup(auditArtifacts);
  useArtifactCleanup(gcpAuditArtifacts);
  useArtifactCleanup(tcoArtifacts);
  useArtifactCleanup(haArtifacts);
  useArtifactCleanup(classicArtifacts);
  useArtifactCleanup(ecrArtifacts);
  useArtifactCleanup(ecsTfArtifacts);
  useArtifactCleanup(ecsManifestArtifacts);
  useArtifactCleanup(eksTfArtifacts);
  useArtifactCleanup(eksManifestArtifacts);
  useArtifactCleanup(vm2gkeArtifacts);
  useArtifactCleanup(boxAwsArtifacts);

  const resolvedRegion = awsRegion === 'custom' ? customRegion.trim() : awsRegion;
  const authReady = Boolean(awsAccess.trim() && awsSecret.trim() && resolvedRegion);
  const auditReady = Boolean(awsAccess.trim() && awsSecret.trim());
  const ecsClusterSelectValue = ecsClusters.includes(ecsClusterName) ? ecsClusterName : '';
  const eksClusterSelectValue = eksClusters.includes(eksClusterName) ? eksClusterName : '';

  const sanitizeNetworkName = (value) =>
    (value || '')
      .toLowerCase()
      .replace(/[^a-z0-9-]/g, '-')
      .replace(/-+/g, '-')
      .replace(/^-|-$/g, '')
      .slice(0, 61);
  const arraysEqual = (a, b) => a.length === b.length && a.every((value, idx) => value === b[idx]);

  const subnetRows = useMemo(() => subnets, [subnets]);
  const gcpSubnetOptions = useMemo(() => {
    const filtered = filterSubnetsByRegion(vpnGcpSubnets, vpnGcpRegion);
    return filtered.length ? filtered : vpnGcpSubnets;
  }, [vpnGcpSubnets, vpnGcpRegion]);
  const vpnViews = ['ha_vpn', 'classic_vpn'];
  const eksClusterViews = ['eks_terraform', 'eks_manifests'];
  const isVpnView = vpnViews.includes(view);
  const vpnLegendLabel = view === 'classic_vpn' ? 'Classic VPN' : 'HA VPN';
  const showAwsSubnetSelector = isVpnView && subnetRows.length > 0;
  const showGcpSubnetSelector = view === 'classic_vpn' && gcpSubnetOptions.length > 0;
  const showGcpSubnetOverlay = view === 'classic_vpn' && vpnSubnetsLoading;

  useEffect(() => {
    if (!resolvedRegion) return;
    const mapped = AWS_TO_GCP_REGION[resolvedRegion];
    if (mapped) {
      setEcrGcpRegion(mapped);
      setEcsTfGcpLocation(mapped);
      setEksTfGcpLocation(mapped);
    }
  }, [resolvedRegion]);

  useEffect(() => {
    if (!authReady) {
      setVpcs([]);
      setSelectedVpc('');
      setSubnets([]);
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const res = await postJson('/api/aws/vpcs/', {
          access_key: awsAccess.trim(),
          secret_key: awsSecret.trim(),
          region: resolvedRegion,
        });
        setVpcs(res.vpcs || []);
        setVpcError('');
      } catch (err) {
        if (!controller.signal.aborted) {
          setVpcError(err.message || String(err));
          setVpcs([]);
        }
      }
    }, debounceDelay);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [awsAccess, awsSecret, awsRegion, customRegion, resolvedRegion, authReady]);

  useEffect(() => {
    if (!authReady || !selectedVpc) {
      setSubnets([]);
      setSelectedAwsSubnets([]);
      setDetectedAwsAsn(null);
      setAttachedVgw(null);
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const res = await postJson('/api/aws/subnets/', {
          access_key: awsAccess.trim(),
          secret_key: awsSecret.trim(),
          region: resolvedRegion,
          vpc_id: selectedVpc,
        });
        const items = (res.subnets || []).map((subnet) => ({
          ...subnet,
          overrideName: subnet.name || '',
          overrideCidr: subnet.cidr || '',
        }));
        setSubnets(items);
        setSelectedAwsSubnets(items.map((item) => item.id));
        setSubnetError('');
        if (res.attached_vgw?.asn) {
          const asn = String(res.attached_vgw.asn);
          const { awsVal, gcpVal } = ensureDifferentAsn(asn, haGcpAsn);
          const asnStr = String(awsVal);
          setHaAwsAsn(asnStr);
          setClassicAwsAsn(asnStr);
          setHaGcpAsn(String(gcpVal));
          setClassicGcpAsn(String(gcpVal));
          setDetectedAwsAsn(asnStr);
          setAttachedVgw(res.attached_vgw);
        } else {
          setDetectedAwsAsn(null);
          setAttachedVgw(null);
        }
      } catch (err) {
        if (!controller.signal.aborted) {
          setSubnetError(err.message || String(err));
          setSubnets([]);
          setSelectedAwsSubnets([]);
          setDetectedAwsAsn(null);
          setAttachedVgw(null);
        }
      }
    }, debounceDelay);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [authReady, selectedVpc, awsAccess, awsSecret, awsRegion, customRegion, resolvedRegion]);

  useEffect(() => {
    if (!selectedVpc) {
      return;
    }
    const match = vpcs.find((vpc) => vpc.id === selectedVpc);
    if (!match) {
      return;
    }
    const suggestion = sanitizeNetworkName(match.name || match.id || '');
    if (suggestion) {
      setGcpNetwork(suggestion);
    }
  }, [selectedVpc, vpcs]);

  useEffect(() => {
    if (!resolvedRegion) {
      return;
    }
    const mapped = AWS_TO_GCP_REGION[resolvedRegion];
    if (mapped) {
      setGcpRegion(mapped);
    }
  }, [resolvedRegion]);

  useEffect(() => {
    if (view !== 'ecr_migration') {
        setEcrRepos([]);
        setSelectedEcrRepos([]);
        setEcrRepoError('');
        return;
    }
    if (!authReady) {
      setEcrRepos([]);
      setSelectedEcrRepos([]);
      setEcrRepoError('');
      return;
    }
    const timer = setTimeout(async () => {
      try {
        const res = await postJson('/api/aws/ecr-repos/', {
          access_key: awsAccess.trim(),
          secret_key: awsSecret.trim(),
          region: resolvedRegion,
        });
        const items = res.repos || [];
        setEcrRepos(items);
        setSelectedEcrRepos(items.map((repo) => repo.name));
        setEcrRepoError('');
      } catch (err) {
        setEcrRepoError(err.message || String(err));
        setEcrRepos([]);
        setSelectedEcrRepos([]);
      }
    }, debounceDelay);
    return () => clearTimeout(timer);
  }, [view, authReady, awsAccess, awsSecret, resolvedRegion]);

  useEffect(() => {
    if (view !== 'ecr_migration') {
      setEcrProjectOptions([]);
      setEcrProjectError('');
      return;
    }
    if (!ecrServiceKey.trim()) {
      setEcrProjectOptions([]);
      setEcrProjectError('');
      setEcrGcpProject('');
      return;
    }
    const timer = setTimeout(async () => {
      try {
        const res = await postJson('/api/gcp/projects/', {
          service_key: ecrServiceKey,
        });
        const options = res.projects || [];
        setEcrProjectOptions(options);
        setEcrProjectError('');
        const existing = ecrGcpProject && options.some((entry) => entry.project_id === ecrGcpProject) ? ecrGcpProject : '';
        const preferred = existing || options[0]?.project_id || res.project_id || '';
        setEcrGcpProject(preferred);
      } catch (err) {
        setEcrProjectError(err.message || String(err));
        setEcrProjectOptions([]);
        setEcrGcpProject('');
      }
    }, debounceDelay);
    return () => clearTimeout(timer);
  }, [ecrServiceKey, view, ecrGcpProject]);

  useEffect(() => {
    if (view !== 'gcp_security_audit') {
      setGcpAuditProjects([]);
      setSelectedGcpAuditProjects([]);
      setGcpAuditProjectError('');
      setGcpAuditProjectsLoading(false);
      return;
    }
    if (!gcpAuditServiceKey.trim()) {
      setGcpAuditProjects([]);
      setSelectedGcpAuditProjects([]);
      setGcpAuditProjectError('');
      setGcpAuditProjectsLoading(false);
      return;
    }
    setGcpAuditProjectsLoading(true);
    const timer = setTimeout(async () => {
      try {
        const res = await postJson('/api/gcp/projects/', {
          service_key: gcpAuditServiceKey,
        });
        const options = res.projects || [];
        setGcpAuditProjects(options);
        setSelectedGcpAuditProjects(options.map((entry) => entry.project_id));
        setGcpAuditProjectError('');
      } catch (err) {
        setGcpAuditProjectError(err.message || String(err));
        setGcpAuditProjects([]);
        setSelectedGcpAuditProjects([]);
      } finally {
        setGcpAuditProjectsLoading(false);
      }
    }, debounceDelay);
    return () => clearTimeout(timer);
  }, [gcpAuditServiceKey, view]);

  useEffect(() => {
    if (!authReady || !['ecs_terraform', 'ecs_manifests'].includes(view)) {
      setEcsClusters([]);
      setEcsClusterError('');
      setEcsClusterLoading(false);
      return;
    }
    setEcsClusterLoading(true);
    const timer = setTimeout(async () => {
      try {
        const res = await postJson('/api/aws/ecs/clusters/', {
          access_key: awsAccess.trim(),
          secret_key: awsSecret.trim(),
          region: resolvedRegion,
        });
        const items = res.clusters || [];
        setEcsClusters(items);
        if (!ecsClusterName && items.length) {
          setEcsClusterName(items[0]);
        }
        setEcsClusterError('');
      } catch (err) {
        setEcsClusterError(err.message || String(err));
        setEcsClusters([]);
      } finally {
        setEcsClusterLoading(false);
      }
    }, debounceDelay);
    return () => clearTimeout(timer);
  }, [authReady, awsAccess, awsSecret, resolvedRegion, view]);

  useEffect(() => {
    if (!authReady || !eksClusterViews.includes(view)) {
      setEksClusterLoading(false);
      if (!eksClusterViews.includes(view)) {
        setEksClusters([]);
        setEksClusterError('');
      }
      return;
    }
    setEksClusterLoading(true);
    const timer = setTimeout(async () => {
      try {
        const res = await postJson('/api/aws/eks/clusters/', {
          access_key: awsAccess.trim(),
          secret_key: awsSecret.trim(),
          region: resolvedRegion,
        });
        const items = res.clusters || [];
        setEksClusters(items);
        setEksClusterName((prev) => {
          if (prev && items.includes(prev)) {
            return prev;
          }
          return items[0] || '';
        });
        setEksClusterError('');
      } catch (err) {
        setEksClusterError(err.message || String(err));
        setEksClusters([]);
      } finally {
        setEksClusterLoading(false);
      }
    }, debounceDelay);
    return () => clearTimeout(timer);
  }, [authReady, awsAccess, awsSecret, resolvedRegion, view]);

  useEffect(() => {
    if (!authReady || !ecsClusterName || !['ecs_terraform', 'ecs_manifests'].includes(view)) {
      setEcsServices([]);
      setEcsServicesError('');
      setEcsServicesLoading(false);
      setEcsTerraformServices([]);
      setEcsManifestServices([]);
      return;
    }
    setEcsServicesLoading(true);
    const timer = setTimeout(async () => {
      try {
        const res = await postJson('/api/aws/ecs/services/', {
          access_key: awsAccess.trim(),
          secret_key: awsSecret.trim(),
          region: resolvedRegion,
          cluster: ecsClusterName,
        });
        const items = res.services || [];
        setEcsServices(items);
        setEcsServicesError('');
        setEcsTerraformServices(items);
        setEcsManifestServices((prev) => (prev.length ? prev.filter((svc) => items.includes(svc)) : items));
      } catch (err) {
        setEcsServicesError(err.message || String(err));
        setEcsServices([]);
        setEcsTerraformServices([]);
        setEcsManifestServices([]);
      } finally {
        setEcsServicesLoading(false);
      }
    }, debounceDelay);
    return () => clearTimeout(timer);
  }, [authReady, awsAccess, awsSecret, resolvedRegion, ecsClusterName, view]);

  useEffect(() => {
    if (!authReady || view !== 'eks_manifests' || !eksClusterName) {
      setEksNamespaces([]);
      setSelectedEksNamespaces([]);
      setEksNamespacesError('');
      setEksNamespacesLoading(false);
      return;
    }
    setEksNamespacesLoading(true);
    const timer = setTimeout(async () => {
      try {
        const res = await postJson('/api/aws/eks/namespaces/', {
          access_key: awsAccess.trim(),
          secret_key: awsSecret.trim(),
          region: resolvedRegion,
          cluster: eksClusterName,
        });
        const items = res.namespaces || [];
        const unique = Array.from(new Set(items)).sort();
        setEksNamespaces(unique);
        setSelectedEksNamespaces(unique);
        setEksNamespacesError('');
      } catch (err) {
        setEksNamespacesError(err.message || String(err));
        setEksNamespaces([]);
        setSelectedEksNamespaces([]);
      } finally {
        setEksNamespacesLoading(false);
      }
    }, debounceDelay);
    return () => clearTimeout(timer);
  }, [authReady, awsAccess, awsSecret, resolvedRegion, eksClusterName, view]);

  // Fetch AWS regions for box_project_aws
  useEffect(() => {
    if (view !== 'box_project_aws') {
      setBoxAwsRegions([]);
      setBoxAwsRegionsLoading(false);
      setBoxAwsRegionsError('');
      return;
    }
    setBoxAwsRegionsLoading(true);
    const timer = setTimeout(async () => {
      try {
        const payload = {};
        // Only include credentials if provided (otherwise use AWS default credential chain)
        if (awsAccess.trim() && awsSecret.trim()) {
          payload.access_key = awsAccess.trim();
          payload.secret_key = awsSecret.trim();
        }
        const res = await postJson('/api/box/aws/regions/', payload);
        const regions = res.regions || [];
        setBoxAwsRegions(regions);
        setBoxAwsRegionsError('');
        if (regions.length > 0 && !regions.includes(boxAwsSelectedRegion)) {
          setBoxAwsSelectedRegion(regions[0]);
        }
      } catch (err) {
        setBoxAwsRegionsError(err.message || String(err));
        setBoxAwsRegions([]);
      } finally {
        setBoxAwsRegionsLoading(false);
      }
    }, debounceDelay);
    return () => clearTimeout(timer);
  }, [view, boxAwsSelectedRegion, awsAccess, awsSecret]);

  // Set default version when OS type changes or on initial load
  useEffect(() => {
    if (view !== 'box_project_aws' || !boxAwsSelectedServices.includes('ec2')) {
      return;
    }
    const defaultVersions = {
      'amazon-linux': '2023',
      'ubuntu': '22.04',
      'windows': '2022',
      'rhel': '9',
      'suse': '15',
      'debian': '12'
    };
    const defaultVersion = defaultVersions[boxAwsEc2OsType] || 'latest';
    if (!boxAwsEc2OsVersion || (boxAwsEc2OsType === 'amazon-linux' && !['2023', '2022', 'latest'].includes(boxAwsEc2OsVersion)) ||
        (boxAwsEc2OsType === 'ubuntu' && !['24.04', '22.04', '20.04', 'latest'].includes(boxAwsEc2OsVersion))) {
      setBoxAwsEc2OsVersion(defaultVersion);
    }
  }, [boxAwsEc2OsType, view, boxAwsSelectedServices]);

  // Fetch EC2 data (AMIs and instance types) for box_project_aws
  useEffect(() => {
    if (view !== 'box_project_aws' || !boxAwsSelectedRegion.trim() || !boxAwsSelectedServices.includes('ec2')) {
      setBoxAwsEc2Data({ amis: [], instance_types: [], instance_type_details: [] });
      setBoxAwsEc2DataLoading(false);
      return;
    }
    setBoxAwsEc2DataLoading(true);
    const timer = setTimeout(async () => {
      try {
        // Only fetch if version is selected
        if (!boxAwsEc2OsVersion) {
          setBoxAwsEc2Data({ amis: [], instance_types: [], instance_type_details: [] });
          setBoxAwsEc2DataLoading(false);
          return;
        }
        const payload = { 
          region: boxAwsSelectedRegion.trim(), 
          os_type: boxAwsEc2OsType,
          os_version: boxAwsEc2OsVersion
        };
        // Only include credentials if provided (otherwise use AWS default credential chain)
        if (awsAccess.trim() && awsSecret.trim()) {
          payload.access_key = awsAccess.trim();
          payload.secret_key = awsSecret.trim();
        }
        const res = await postJson('/api/box/aws/ec2-data/', payload);
        console.log('EC2 data response:', res);
        setBoxAwsEc2Data({
          amis: res.amis || [],
          instance_types: res.instance_types || [],
          instance_type_details: res.instance_type_details || [],
        });
      } catch (err) {
        console.error('Failed to fetch EC2 data:', err);
        setBoxAwsEc2Data({ amis: [], instance_types: [], instance_type_details: [] });
      } finally {
        setBoxAwsEc2DataLoading(false);
      }
    }, debounceDelay);
    return () => clearTimeout(timer);
  }, [authReady, awsAccess, awsSecret, boxAwsSelectedRegion, boxAwsSelectedServices, boxAwsEc2OsType, boxAwsEc2OsVersion, view]);

  // Fetch RDS data (engines and instance classes) for box_project_aws
  useEffect(() => {
    if (view !== 'box_project_aws' || !boxAwsSelectedRegion.trim() || !boxAwsSelectedServices.includes('rds')) {
      setBoxAwsRdsData({ engines: [], instance_classes: [] });
      setBoxAwsRdsDataLoading(false);
      return;
    }
    setBoxAwsRdsDataLoading(true);
    const timer = setTimeout(async () => {
      try {
        const engine = boxAwsServiceConfigs.rds?.engine || 'mysql';
        const payload = {
          region: boxAwsSelectedRegion.trim(),
          engine: engine,
        };
        // Only include credentials if provided (otherwise use AWS default credential chain)
        if (awsAccess.trim() && awsSecret.trim()) {
          payload.access_key = awsAccess.trim();
          payload.secret_key = awsSecret.trim();
        }
        const res = await postJson('/api/box/aws/rds-data/', payload);
        setBoxAwsRdsData({
          engines: res.engines || [],
          instance_classes: res.instance_classes || [],
        });
      } catch (err) {
        setBoxAwsRdsData({ engines: [], instance_classes: [] });
      } finally {
        setBoxAwsRdsDataLoading(false);
      }
    }, debounceDelay);
    return () => clearTimeout(timer);
  }, [boxAwsSelectedRegion, boxAwsSelectedServices, boxAwsServiceConfigs.rds?.engine, view, awsAccess, awsSecret]);

  // Fetch availability zones for box_project_aws (for EC2 additional volumes)
  useEffect(() => {
    if (view !== 'box_project_aws' || !boxAwsSelectedRegion.trim() || !boxAwsSelectedServices.includes('ec2')) {
      setBoxAwsAvailabilityZones([]);
      setBoxAwsAvailabilityZonesLoading(false);
      return;
    }
    setBoxAwsAvailabilityZonesLoading(true);
    const timer = setTimeout(async () => {
      try {
        const payload = { region: boxAwsSelectedRegion.trim() };
        if (awsAccess.trim() && awsSecret.trim()) {
          payload.access_key = awsAccess.trim();
          payload.secret_key = awsSecret.trim();
        }
        const res = await postJson('/api/box/aws/availability-zones/', payload);
        setBoxAwsAvailabilityZones(res.availability_zones || []);
      } catch (err) {
        console.error('Failed to fetch availability zones:', err);
        setBoxAwsAvailabilityZones([]);
      } finally {
        setBoxAwsAvailabilityZonesLoading(false);
      }
    }, debounceDelay);
    return () => clearTimeout(timer);
  }, [view, awsAccess, awsSecret, boxAwsSelectedRegion, boxAwsSelectedServices]);


  useEffect(() => {
    if (view !== 'vm2gke_manifests') {
      setVm2gkeInstances([]);
      setVm2gkeInstancesError('');
      setVm2gkeInstancesLoading(false);
      setVm2gkeSelectedInstance(null);
      setVm2gkeDockerContainers([]);
      setVm2gkeDockerImages([]);
      setVm2gkeDockerEnvVars({});
      return;
    }
    setVm2gkeInstancesLoading(true);
    const timer = setTimeout(async () => {
      try {
        let res;
        if (vm2gkeProvider === 'aws') {
          if (!awsAccess.trim() || !awsSecret.trim() || !vm2gkeAwsRegion.trim()) {
            setVm2gkeInstances([]);
            setVm2gkeInstancesError('');
            setVm2gkeInstancesLoading(false);
            return;
          }
          res = await postJson('/api/aws/ec2/instances/', {
            access_key: awsAccess.trim(),
            secret_key: awsSecret.trim(),
            region: vm2gkeAwsRegion,
          });
        } else {
          if (!vm2gkeGcpServiceKey.trim() || !vm2gkeGcpProject.trim() || !vm2gkeGcpRegion.trim()) {
            setVm2gkeInstances([]);
            setVm2gkeInstancesError('');
            setVm2gkeInstancesLoading(false);
            return;
          }
          res = await postJson('/api/gcp/compute/instances/', {
            service_key: vm2gkeGcpServiceKey.trim(),
            project: vm2gkeGcpProject,
            zone: vm2gkeGcpRegion,
          });
        }
        const items = res.instances || [];
        setVm2gkeInstances(items);
        setVm2gkeInstancesError('');
        // Don't auto-select, let user choose
      } catch (err) {
        setVm2gkeInstancesError(err.message || String(err));
        setVm2gkeInstances([]);
        setVm2gkeSelectedInstance(null);
      } finally {
        setVm2gkeInstancesLoading(false);
      }
    }, debounceDelay);
    return () => clearTimeout(timer);
  }, [view, vm2gkeProvider, awsAccess, awsSecret, vm2gkeAwsRegion, vm2gkeGcpServiceKey, vm2gkeGcpProject, vm2gkeGcpRegion]);

  // Function to fetch Docker containers (called when "Proceed" button is clicked)
  const fetchDockerContainers = async () => {
    if (view !== 'vm2gke_manifests' || !vm2gkeSelectedInstance) {
      return;
    }
    
    // Clear Docker data and start loading
    setVm2gkeDockerContainers([]);
    setVm2gkeSelectedContainers([]);
    setVm2gkeDockerImages([]);
    setVm2gkeDockerEnvVars({});
    setVm2gkeDockerError('');
    setVm2gkeDockerLoading(true);
    setVm2gkeDockerDiscoveryInitiated(true);
    
    try {
      let res;
        if (vm2gkeProvider === 'aws') {
          if (!awsAccess.trim() || !awsSecret.trim() || !vm2gkeAwsRegion.trim()) {
            setVm2gkeDockerContainers([]);
            setVm2gkeDockerImages([]);
            setVm2gkeDockerEnvVars({});
            setVm2gkeDockerLoading(false);
            return;
          }
          const instance = vm2gkeInstances.find((inst) => inst.name === vm2gkeSelectedInstance);
          if (!instance) {
            setVm2gkeDockerContainers([]);
            setVm2gkeDockerLoading(false);
            return;
          }
          res = await postJson('/api/aws/ec2/docker/', {
            instance_id: instance.id,
            region: vm2gkeAwsRegion,
            access_key: awsAccess.trim(),
            secret_key: awsSecret.trim(),
          });
        } else {
          if (!vm2gkeGcpServiceKey.trim() || !vm2gkeGcpProject.trim() || !vm2gkeGcpRegion.trim()) {
            setVm2gkeDockerContainers([]);
            setVm2gkeDockerImages([]);
            setVm2gkeDockerEnvVars({});
            setVm2gkeDockerLoading(false);
            return;
          }
          const instance = vm2gkeInstances.find((inst) => inst.name === vm2gkeSelectedInstance);
          if (!instance || !instance.zone) {
            setVm2gkeDockerError('Instance zone information is missing. Please ensure the instance has a valid zone.');
            setVm2gkeDockerContainers([]);
            setVm2gkeDockerLoading(false);
            return;
          }
          res = await postJson('/api/gcp/compute/docker/', {
            instance_name: vm2gkeSelectedInstance,
            project: vm2gkeGcpProject,
            zone: instance.zone,
            service_key: vm2gkeGcpServiceKey.trim(),
          });
        }
        const containers = res.containers || [];
        setVm2gkeDockerContainers(containers);
        // Auto-select all containers by default
        setVm2gkeSelectedContainers(containers.map((c) => c.name));
        setVm2gkeDockerImages(res.images || []);
        setVm2gkeDockerEnvVars(res.env_vars || {});
        setVm2gkeDockerError('');
    } catch (err) {
      setVm2gkeDockerError(err.message || String(err));
      setVm2gkeDockerContainers([]);
      setVm2gkeSelectedContainers([]);
      setVm2gkeDockerImages([]);
      setVm2gkeDockerEnvVars({});
    } finally {
      setVm2gkeDockerLoading(false);
    }
  };

  // Clear Docker data when instance changes or view changes
  useEffect(() => {
    if (view !== 'vm2gke_manifests' || !vm2gkeSelectedInstance) {
      setVm2gkeDockerContainers([]);
      setVm2gkeSelectedContainers([]);
      setVm2gkeDockerImages([]);
      setVm2gkeDockerEnvVars({});
      setVm2gkeDockerError('');
      setVm2gkeDockerLoading(false);
      setVm2gkeDockerDiscoveryInitiated(false);
      return;
    }
  }, [view, vm2gkeSelectedInstance]);

  useEffect(() => {
    if (view !== 'vm2gke_manifests' || vm2gkeProvider !== 'gcp') {
      setVm2gkeGcpProjectOptions([]);
      setVm2gkeGcpProjectError('');
      setVm2gkeGcpProjectsLoading(false);
      return;
    }
    if (!vm2gkeGcpServiceKey.trim()) {
      setVm2gkeGcpProjectOptions([]);
      setVm2gkeGcpProjectError('');
      setVm2gkeGcpProjectsLoading(false);
      setVm2gkeGcpProject('');
      return;
    }
    setVm2gkeGcpProjectsLoading(true);
    const timer = setTimeout(async () => {
      try {
        const res = await postJson('/api/gcp/projects/', {
          service_key: vm2gkeGcpServiceKey,
        });
        const options = res.projects || [];
        setVm2gkeGcpProjectOptions(options);
        setVm2gkeGcpProjectError('');
        const existing = vm2gkeGcpProject && options.some((entry) => entry.project_id === vm2gkeGcpProject) ? vm2gkeGcpProject : '';
        const preferred = existing || options[0]?.project_id || res.project_id || '';
        setVm2gkeGcpProject(preferred);
      } catch (err) {
        setVm2gkeGcpProjectError(err.message || String(err));
        setVm2gkeGcpProjectOptions([]);
        setVm2gkeGcpProject('');
      } finally {
        setVm2gkeGcpProjectsLoading(false);
      }
    }, debounceDelay);
    return () => clearTimeout(timer);
  }, [view, vm2gkeProvider, vm2gkeGcpServiceKey]);

  useEffect(() => {
    if (!isVpnView) {
      setVpnProjectOptions([]);
      setVpnProjectError('');
      return;
    }
    if (!vpnServiceKey.trim()) {
      setVpnProjectOptions([]);
      setVpnProjectError('');
      setVpnGcpProject('');
      return;
    }
    const timer = setTimeout(async () => {
      try {
        const res = await postJson('/api/gcp/projects/', {
          service_key: vpnServiceKey,
        });
        const options = res.projects || [];
        setVpnProjectOptions(options);
        setVpnProjectError('');
        const existing = vpnGcpProject && options.some((entry) => entry.project_id === vpnGcpProject) ? vpnGcpProject : '';
        const preferred = existing || options[0]?.project_id || res.project_id || '';
        setVpnGcpProject(preferred);
      } catch (err) {
        setVpnProjectError(err.message || String(err));
        setVpnProjectOptions([]);
        setVpnGcpProject('');
      }
    }, debounceDelay);
    return () => {
      clearTimeout(timer);
    };
  }, [vpnServiceKey, view, isVpnView]);

  useEffect(() => {
    if (!isVpnView) {
      return;
    }
    if (!vpnServiceKey.trim()) {
      setVpnGcpNetworks([]);
      setVpnGcpNetwork('');
      setVpnNetworkError('');
      return;
    }
    if (!vpnGcpProject.trim()) {
      setVpnGcpNetworks([]);
      setVpnGcpNetwork('');
      return;
    }
    const trimmedProject = vpnGcpProject.trim();
    const timer = setTimeout(async () => {
      try {
        const res = await postJson('/api/gcp/networks/', {
          service_key: vpnServiceKey,
          gcp_project: trimmedProject,
        });
        const networks = res.networks || [];
        setVpnGcpNetworks(networks);
        setVpnNetworkError('');
        const stillValid = vpnGcpNetwork && networks.some((n) => n.name === vpnGcpNetwork);
        if (!stillValid) {
          setVpnGcpNetwork('');
        }
      } catch (err) {
        setVpnNetworkError(err.message || String(err));
        setVpnGcpNetworks([]);
        setVpnGcpNetwork('');
      }
    }, debounceDelay);
    return () => {
      clearTimeout(timer);
    };
  }, [vpnServiceKey, vpnGcpProject, vpnGcpNetwork, view, isVpnView]);

  useEffect(() => {
    const requestId = ++gcpSubnetRequestRef.current;
    const shouldLoadSubnets = view === 'classic_vpn';
    if (!shouldLoadSubnets || !isVpnView) {
      setVpnGcpSubnets([]);
      setSelectedGcpSubnets([]);
      setVpnSubnetError('');
      setVpnSubnetsLoading(false);
      return;
    }
    const trimmedProject = vpnGcpProject.trim();
    if (!vpnServiceKey.trim() || !trimmedProject || !vpnGcpNetwork) {
      setVpnGcpSubnets([]);
      setSelectedGcpSubnets([]);
      setVpnSubnetError('');
      setVpnSubnetsLoading(false);
      return;
    }
    const cacheKey = `${trimmedProject}::${vpnGcpNetwork}::${vpnGcpRegion}`;
    const cached = gcpSubnetCacheRef.current.get(cacheKey);
    if (cached) {
      setVpnGcpSubnets(cached);
      setSelectedGcpSubnets(cached.map((entry) => entry.name));
      setVpnSubnetError('');
      setVpnSubnetsLoading(false);
      return;
    }
    setVpnSubnetsLoading(true);
    setVpnSubnetError('');
    const timer = setTimeout(async () => {
      try {
        const res = await postJson('/api/gcp/network/', {
          service_key: vpnServiceKey,
          gcp_project: trimmedProject,
          gcp_network: vpnGcpNetwork,
          gcp_region: vpnGcpRegion,
        });
        const subnets = res.network?.subnetworks || [];
        gcpSubnetCacheRef.current.set(cacheKey, subnets);
        if (gcpSubnetRequestRef.current !== requestId) {
          return;
        }
        const filtered = filterSubnetsByRegion(subnets, vpnGcpRegion);
        setVpnGcpSubnets(subnets);
        setSelectedGcpSubnets((filtered.length ? filtered : subnets).map((entry) => entry.name));
        setVpnSubnetError('');
      } catch (err) {
        if (gcpSubnetRequestRef.current !== requestId) {
          return;
        }
        setVpnSubnetError(err.message || String(err));
        gcpSubnetCacheRef.current.delete(cacheKey);
        setVpnGcpSubnets([]);
        setSelectedGcpSubnets([]);
      } finally {
        if (gcpSubnetRequestRef.current === requestId) {
          setVpnSubnetsLoading(false);
        }
      }
    }, debounceDelay);
    return () => {
      clearTimeout(timer);
      // If we switch region/network before the fetch fires, clear any stale loading overlay.
      setVpnSubnetsLoading(false);
    };
  }, [view, vpnServiceKey, vpnGcpProject, vpnGcpNetwork, vpnGcpRegion, isVpnView]);
  useEffect(() => {
    if (!vpnGcpSubnets.length) {
      return;
    }
    const allowed = filterSubnetsByRegion(vpnGcpSubnets, vpnGcpRegion);
    if (!allowed.length) {
      setSelectedGcpSubnets([]);
      return;
    }
    const allowedNames = new Set(allowed.map((entry) => entry.name));
    setSelectedGcpSubnets((prev) => {
      const next = prev.filter((name) => allowedNames.has(name));
      if (next.length) {
        return arraysEqual(next, prev) ? prev : next;
      }
      const allAllowed = Array.from(allowedNames);
      return arraysEqual(prev, allAllowed) ? prev : allAllowed;
    });
  }, [vpnGcpRegion, vpnGcpSubnets]);
  useEffect(() => {
    if (!resolvedRegion) {
      return;
    }
    const mapped = AWS_TO_GCP_REGION[resolvedRegion];
    if (mapped) {
      setVpnGcpRegion(mapped);
    }
  }, [resolvedRegion]);

  const allRegionsSelected = invRegions.length === AWS_REGION_CHOICES.length && invRegions.length > 0;
  const allResourcesSelected = invResources.length === INVENTORY_RESOURCE_IDS.length && invResources.length > 0;

  const collectOverrides = () => {
    const nameMap = {};
    const cidrMap = {};
    subnetRows.forEach((subnet) => {
      if (subnet.overrideName && subnet.overrideName !== (subnet.name || '')) {
        nameMap[subnet.id] = subnet.overrideName;
      }
      if (subnet.overrideCidr && subnet.overrideCidr !== subnet.cidr) {
        cidrMap[subnet.id] = subnet.overrideCidr;
      }
    });
    return {
      subnet_name_map: Object.keys(nameMap).length ? JSON.stringify(nameMap) : undefined,
      subnet_cidr_map: Object.keys(cidrMap).length ? JSON.stringify(cidrMap) : undefined,
    };
  };

  const handleSubnetChange = (idx, field, value) => {
    setSubnets((prev) => prev.map((item, index) =>
      index === idx ? { ...item, [field]: value } : item
    ));
  };

  const toggleManifestService = (service) => {
    setEcsManifestServices((prev) =>
      prev.includes(service) ? prev.filter((item) => item !== service) : [...prev, service]
    );
  };

  const selectAllManifestServices = () => setEcsManifestServices(ecsServices);
  const clearManifestServices = () => setEcsManifestServices([]);
  const toggleEksNamespace = (namespace) => {
    setSelectedEksNamespaces((prev) =>
      prev.includes(namespace) ? prev.filter((item) => item !== namespace) : [...prev, namespace]
    );
  };
  const selectAllEksNamespaces = () => setSelectedEksNamespaces(eksNamespaces.slice());
  const clearEksNamespaces = () => setSelectedEksNamespaces([]);
  const toggleEksResourceType = (resource) => {
    setSelectedEksResourceTypes((prev) =>
      prev.includes(resource) ? prev.filter((item) => item !== resource) : [...prev, resource]
    );
  };
  const selectAllEksResourceTypes = () => setSelectedEksResourceTypes(DEFAULT_EKS_RESOURCE_TYPES.slice());
  const clearEksResourceTypes = () => setSelectedEksResourceTypes([]);
  const runTerraformTask = async () => {
    setTfError('');
    setTfLogs('Submitting Terraform bundle request...\n');
    setTfArtifacts([]);
    if (!authReady || !selectedVpc || !gcpProject.trim() || !gcpNetwork.trim() || !gcpRegion.trim()) {
      setTfError('AWS creds, VPC, and GCP fields are required.');
      return;
    }
    try {
      const overrides = collectOverrides();
      const event = await runStreamingTask(
        '/api/tasks/run-stream/',
        {
          task_id: 'terraform_vpc',
          data: {
            access_key: awsAccess.trim(),
            secret_key: awsSecret.trim(),
            aws_region: resolvedRegion,
            aws_vpc_id: selectedVpc,
            gcp_project: gcpProject.trim(),
            gcp_network: gcpNetwork.trim(),
            gcp_region_fallback: gcpRegion.trim(),
            ...overrides,
          },
        },
        (message) => setTfLogs((prev) => mergeBackendLogs(prev, message))
      );
      setTfArtifacts(createDownloadEntries(event.artifacts || []));
    } catch (err) {
      setTfError(err.message || String(err));
    }
  };

  const runEcsTerraformTask = async () => {
    setEcsTfError('');
    setEcsTfLogs('Planning ECS → GKE Terraform bundle...\n');
    setEcsTfArtifacts([]);
    if (!authReady || !ecsClusterName || !ecsTfGcpProject.trim() || !ecsTfGcpLocation.trim()) {
      setEcsTfError('AWS creds, ECS cluster, and GCP fields are required.');
      return;
    }
    try {
      const payload = {
        access_key: awsAccess.trim(),
        secret_key: awsSecret.trim(),
        aws_region: resolvedRegion,
        cluster_name: ecsClusterName,
        gcp_project: ecsTfGcpProject.trim(),
        gcp_location: ecsTfGcpLocation.trim(),
        gke_cluster_name: ecsTfGkeName.trim(),
        machine_type: ecsTfMachineType.trim(),
        node_cpu: ecsTfNodeCpu,
        node_memory: ecsTfNodeMem,
        min_nodes: ecsTfMinNodes,
        max_nodes: ecsTfMaxNodes,
        node_locations: ecsTfNodeLocations,
        network: ecsTfNetwork.trim(),
        subnetwork: ecsTfSubnetwork.trim(),
        service_account: ecsTfServiceAccount.trim(),
        release_channel: ecsTfReleaseChannel.trim(),
        private_nodes: ecsTfPrivateNodes,
        private_endpoint: ecsTfPrivateEndpoint,
        master_ipv4_cidr: ecsTfMasterCidr.trim(),
        node_pool_name: ecsTfNodePoolName.trim(),
        node_pool_subnet: ecsTfNodePoolSubnet.trim(),
        node_pool_zones: ecsTfNodePoolZones,
        services: ecsTerraformServices,
      };
      const event = await runStreamingTask(
        '/api/tasks/run-stream/',
        { task_id: 'ecs_terraform', data: payload },
        (message) => setEcsTfLogs((prev) => mergeBackendLogs(prev, message))
      );
      setEcsTfArtifacts(createDownloadEntries(event.artifacts || []));
    } catch (err) {
      setEcsTfError(err.message || String(err));
    }
  };

  const runEksTerraformTask = async () => {
    setEksTfError('');
    setEksTfLogs('Planning EKS -> GKE Terraform bundle...\n');
    setEksTfArtifacts([]);
    if (!authReady || !eksClusterName || !eksTfGcpProject.trim() || !eksTfGcpLocation.trim()) {
      setEksTfError('AWS creds, EKS cluster, and GCP fields are required.');
      return;
    }
    try {
      const payload = {
        access_key: awsAccess.trim(),
        secret_key: awsSecret.trim(),
        aws_region: resolvedRegion,
        cluster_name: eksClusterName,
        gcp_project: eksTfGcpProject.trim(),
        gcp_location: eksTfGcpLocation.trim(),
        gke_cluster_name: eksTfGkeName.trim(),
        machine_type: eksTfMachineType.trim(),
        node_cpu: eksTfNodeCpu,
        node_memory: eksTfNodeMem,
        min_nodes: eksTfMinNodes,
        max_nodes: eksTfMaxNodes,
        node_locations: eksTfNodeLocations,
        network: eksTfNetwork.trim(),
        subnetwork: eksTfSubnetwork.trim(),
        service_account: eksTfServiceAccount.trim(),
        release_channel: eksTfReleaseChannel.trim(),
        private_nodes: eksTfPrivateNodes,
        private_endpoint: eksTfPrivateEndpoint,
        master_ipv4_cidr: eksTfMasterCidr.trim(),
      };
      const event = await runStreamingTask(
        '/api/tasks/run-stream/',
        { task_id: 'eks_terraform', data: payload },
        (message) => setEksTfLogs((prev) => mergeBackendLogs(prev, message))
      );
      setEksTfArtifacts(createDownloadEntries(event.artifacts || []));
    } catch (err) {
      setEksTfError(err.message || String(err));
    }
  };

  const runEcsManifestTask = async () => {
    setEcsManifestError('');
    setEcsManifestLogs('Submitting ECS → GKE manifest request...\n');
    setEcsManifestArtifacts([]);
    if (!authReady || !ecsClusterName) {
      setEcsManifestError('AWS creds and ECS cluster are required.');
      return;
    }
    try {
      const payload = {
        access_key: awsAccess.trim(),
        secret_key: awsSecret.trim(),
        aws_region: resolvedRegion,
        cluster_name: ecsClusterName,
        services: ecsManifestServices,
      };
      const event = await runStreamingTask(
        '/api/tasks/run-stream/',
        { task_id: 'ecs_manifests', data: payload },
        (message) => setEcsManifestLogs((prev) => mergeBackendLogs(prev, message))
      );
      setEcsManifestArtifacts(createDownloadEntries(event.artifacts || []));
    } catch (err) {
      setEcsManifestError(err.message || String(err));
    }
  };

  const runEksManifestTask = async () => {
    setEksManifestError('');
    setEksManifestLogs('Exporting EKS namespaces...\n');
    setEksManifestArtifacts([]);
    if (!authReady || !eksClusterName || !selectedEksNamespaces.length) {
      setEksManifestError('AWS creds, EKS cluster, and at least one namespace are required.');
      return;
    }
    if (!selectedEksResourceTypes.length) {
      setEksManifestError('Select at least one resource type.');
      return;
    }
    try {
      const payload = {
        access_key: awsAccess.trim(),
        secret_key: awsSecret.trim(),
        aws_region: resolvedRegion,
        cluster_name: eksClusterName,
        namespaces: selectedEksNamespaces,
        resource_types: selectedEksResourceTypes.join(','),
      };
      const event = await runStreamingTask(
        '/api/tasks/run-stream/',
        { task_id: 'eks_manifests', data: payload },
        (message) => setEksManifestLogs((prev) => mergeBackendLogs(prev, message))
      );
      setEksManifestArtifacts(createDownloadEntries(event.artifacts || []));
    } catch (err) {
      setEksManifestError(err.message || String(err));
    }
  };

  const runVm2GkeManifestTask = async () => {
    setVm2gkeError('');
    setVm2gkeLogs('Submitting VM → GKE manifest request...\n');
    setVm2gkeArtifacts([]);
    
    if (vm2gkeProvider === 'aws') {
      if (!authReady || !vm2gkeAwsRegion.trim()) {
        setVm2gkeError('AWS creds and region are required.');
        return;
      }
    } else {
      if (!vm2gkeGcpServiceKey.trim() || !vm2gkeGcpProject.trim()) {
        setVm2gkeError('GCP service account key and project are required.');
        return;
      }
    }
    
    if (!vm2gkeSelectedInstance) {
      setVm2gkeError('Select a VM instance.');
      return;
    }
    
    try {
      if (!vm2gkeSelectedContainers.length) {
        setVm2gkeError('Select at least one Docker container to migrate.');
        return;
      }
      
      const payload = {
        provider: vm2gkeProvider,
        access_key: vm2gkeProvider === 'aws' ? awsAccess.trim() : '',
        secret_key: vm2gkeProvider === 'aws' ? awsSecret.trim() : '',
        aws_region: vm2gkeProvider === 'aws' ? vm2gkeAwsRegion : '',
        gcp_service_key: vm2gkeProvider === 'gcp' ? vm2gkeGcpServiceKey.trim() : '',
        gcp_project: vm2gkeProvider === 'gcp' ? vm2gkeGcpProject : '',
        gcp_region: vm2gkeProvider === 'gcp' ? vm2gkeGcpRegion : '',
        instance: vm2gkeSelectedInstance,
        selected_containers: vm2gkeSelectedContainers,
        namespace: vm2gkeNamespace.trim() || undefined,
      };
      const event = await runStreamingTask(
        '/api/tasks/run-stream/',
        { task_id: 'vm2gke_manifests', data: payload },
        (message) => setVm2gkeLogs((prev) => mergeBackendLogs(prev, message))
      );
      setVm2gkeArtifacts(createDownloadEntries(event.artifacts || []));
    } catch (err) {
      setVm2gkeError(err.message || String(err));
    }
  };

  const runBoxProjectAwsTask = async () => {
    setBoxAwsError('');
    setBoxAwsLogs('Preparing AWS Terraform project with boto3...\n');
    setBoxAwsArtifacts([]);
    if (!boxAwsSelectedServices.length) {
      setBoxAwsError('Select at least one service.');
      return;
    }
    if (!boxAwsSelectedRegion.trim()) {
      setBoxAwsError('AWS region is required.');
      return;
    }
    
    // Build service configs with EC2 instances and EBS volumes
    const serviceConfigs = { ...boxAwsServiceConfigs };
    
    // Add EC2 instances data if EC2 is selected
    if (boxAwsSelectedServices.includes('ec2')) {
      serviceConfigs.ec2 = {
        ...serviceConfigs.ec2,
        instances: serviceConfigs.ec2?.instances || {},
      };
      // Ensure all instances have at least basic config
      boxAwsEc2Instances.forEach(inst => {
        if (!serviceConfigs.ec2.instances[inst.id]) {
          serviceConfigs.ec2.instances[inst.id] = {
            name: inst.name || `instance-${inst.id}`,
            ami: serviceConfigs.ec2?.ami || boxAwsEc2SelectedAmi?.id || '',
            instance_type: serviceConfigs.ec2?.instance_type || 't3.micro',
            root_volume_size: boxAwsEc2StorageSize || 8,
            root_volume_type: boxAwsEc2StorageType || 'gp3',
          };
        }
      });
    }
    
    const payload = {
      aws_region: boxAwsSelectedRegion.trim(),
      services: boxAwsSelectedServices,
      service_configs: serviceConfigs,
    };
    // Only include credentials if provided (otherwise use environment variables)
    if (awsAccess.trim() && awsSecret.trim()) {
      payload.access_key = awsAccess.trim();
      payload.secret_key = awsSecret.trim();
    }
    try {
      const event = await runStreamingTask(
        '/api/tasks/run-stream/',
        { task_id: 'box_project_aws', data: payload },
        (message) => setBoxAwsLogs((prev) => mergeBackendLogs(prev, message))
      );
      setBoxAwsArtifacts(createDownloadEntries(event.artifacts || []));
    } catch (err) {
      setBoxAwsError(err.message || String(err));
    }
  };

  const appendInvLog = (message) => {
    const line = `[${new Date().toLocaleTimeString()}] ${message}`;
    setInvLogs((prev) => (prev ? `${prev}\n${line}` : line));
  };

  const stopInvProgress = () => {
    if (invProgressTimer.current) {
      clearInterval(invProgressTimer.current);
      invProgressTimer.current = null;
    }
  };

  useEffect(() => () => stopInvProgress(), []);

  const startInvProgress = (regions, resourceCount) => {
    stopInvProgress();
    const dynamicMessages = [
      'Connecting to AWS...',
      `Checking ${resourceCount} resource type(s)...`,
      ...regions.map((regionId) => `Collecting data for ${regionLabel(regionId)}...`),
      'Packaging XLSX files...',
    ];
    let index = 0;
    if (dynamicMessages.length) {
      appendInvLog(dynamicMessages[index]);
      index += 1;
    }
    invProgressTimer.current = setInterval(() => {
      if (index >= dynamicMessages.length) {
        stopInvProgress();
        return;
      }
      appendInvLog(dynamicMessages[index]);
      index += 1;
    }, 1500);
  };

  const mergeBackendLogs = (existing, incoming) => {
    const cleanIncoming = (incoming || '').trim();
    if (!cleanIncoming) {
      return existing;
    }
    return existing ? `${existing}\n${cleanIncoming}` : cleanIncoming;
  };

  const handleServiceKeyFile = (file) => {
    if (!file) {
      clearServiceKey();
      return;
    }
    clearServiceKey();
    setVpnServiceFileName(file.name);
    const reader = new FileReader();
    reader.onload = (event) => {
      setVpnServiceKey(event.target?.result?.toString() || '');
    };
    reader.readAsText(file);
  };

  const clearServiceKey = () => {
    setVpnServiceKey('');
    setVpnServiceFileName('');
    setVpnGcpNetworks([]);
    setVpnGcpNetwork('');
    setSelectedGcpSubnets([]);
    setVpnProjectOptions([]);
    setVpnProjectError('');
    setVpnGcpProject('');
    setVpnNetworkError('');
    setVpnGcpSubnets([]);
    setVpnSubnetError('');
    setHaLogs('');
    setHaArtifacts([]);
    setHaError('');
    setClassicLogs('');
    setClassicArtifacts([]);
    setClassicError('');
  };

  const runHaVpnTask = async () => {
    setHaError('');
    setHaLogs('Submitting HA VPN setup request...\n');
    setHaArtifacts([]);
    if (!authReady || !selectedVpc || !vpnServiceKey.trim() || !vpnGcpProject.trim() || !vpnGcpNetwork) {
      setHaError('AWS creds, selected VPC, service key, project, and GCP network are required.');
      return;
    }
    const awsAsnValue = Number(haAwsAsn) || 64513;
    const gcpAsnValue = Number(haGcpAsn) || 64512;
    try {
      const event = await runStreamingTask(
        '/api/tasks/run-stream/',
        {
          task_id: 'ha_vpn',
          data: {
            access_key: awsAccess.trim(),
            secret_key: awsSecret.trim(),
            aws_region: resolvedRegion,
            aws_vpc_id: selectedVpc,
            gcp_service_key: vpnServiceKey,
            gcp_project: vpnGcpProject.trim(),
            gcp_region: vpnGcpRegion,
            gcp_network: vpnGcpNetwork,
            aws_asn: awsAsnValue,
            gcp_asn: gcpAsnValue,
            name_prefix: haPrefix.trim(),
            aws_subnets: selectedAwsSubnets,
          },
        },
        (message) => setHaLogs((prev) => mergeBackendLogs(prev, message)),
      );
      setHaArtifacts(createDownloadEntries(event.artifacts || []));
    } catch (err) {
      setHaError(err.message || String(err));
    }
  };

  const runClassicVpnTask = async () => {
    setClassicError('');
    setClassicLogs('Submitting Classic VPN setup request...\n');
    setClassicArtifacts([]);
    if (!authReady || !selectedVpc || !vpnServiceKey.trim() || !vpnGcpProject.trim() || !vpnGcpNetwork) {
      setClassicError('AWS creds, selected VPC, service key, project, and GCP network are required.');
      return;
    }
    if (!selectedAwsSubnets.length || !selectedGcpSubnets.length) {
      setClassicError('Select at least one subnet in both AWS and GCP.');
      return;
    }
    const awsAsnValue = Number(classicAwsAsn) || 64513;
    const gcpAsnValue = Number(classicGcpAsn) || 64512;
    const ikeValue = Math.min(2, Math.max(1, Number(classicIkeVersion) || 1));
    try {
      const event = await runStreamingTask(
        '/api/tasks/run-stream/',
        {
          task_id: 'classic_vpn',
          data: {
            access_key: awsAccess.trim(),
            secret_key: awsSecret.trim(),
            aws_region: resolvedRegion,
            aws_vpc_id: selectedVpc,
            gcp_service_key: vpnServiceKey,
            gcp_project: vpnGcpProject.trim(),
            gcp_region: vpnGcpRegion,
            gcp_network: vpnGcpNetwork,
            aws_asn: awsAsnValue,
            gcp_asn: gcpAsnValue,
            name_prefix: classicPrefix.trim(),
            ike_version: ikeValue,
            aws_subnets: selectedAwsSubnets,
            gcp_subnets: selectedGcpSubnets,
          },
        },
        (message) => setClassicLogs((prev) => mergeBackendLogs(prev, message)),
      );
      setClassicArtifacts(createDownloadEntries(event.artifacts || []));
    } catch (err) {
      setClassicError(err.message || String(err));
    }
  };

  const runEcrMigration = async () => {
    setEcrError('');
    setEcrLogs('Submitting ECR to Artifact Registry migration...\n');
    setEcrArtifacts([]);
    if (!awsAccess.trim() || !awsSecret.trim() || !resolvedRegion || !ecrGcpProject.trim() || !ecrGcpRegion.trim()) {
      setEcrError('AWS creds, AWS region, GCP project, and GCP region are required.');
      return;
    }
    if (!ecrServiceKey.trim()) {
      setEcrError('Upload a GCP service account JSON key.');
      return;
    }
    if (!selectedEcrRepos.length) {
      setEcrError('Select at least one ECR repository to migrate.');
      return;
    }
    try {
      const event = await runStreamingTask(
        '/api/tasks/run-stream/',
        {
          task_id: 'ecr_migration',
          data: {
            access_key: awsAccess.trim(),
            secret_key: awsSecret.trim(),
            aws_region: resolvedRegion,
            gcp_project: ecrGcpProject.trim(),
            gcp_region: ecrGcpRegion.trim(),
            workers: Math.min(Number(ecrWorkers) || 1, ecrMaxWorkers),
            aws_repos: selectedEcrRepos,
            gcp_service_key: ecrServiceKey,
          },
        },
        (message) => setEcrLogs((prev) => mergeBackendLogs(prev, message)),
      );
      setEcrArtifacts(createDownloadEntries(event.artifacts || []));
    } catch (err) {
      setEcrError(err.message || String(err));
    }
  };

  const runInventory = async () => {
    setInvError('');
    setInvLogs('');
    setInvArtifacts([]);
    setInvStatus('');
    if (!awsAccess.trim() || !awsSecret.trim()) {
      setInvError('AWS access key and secret are required.');
      setInvStatus('Missing AWS credentials.');
      return;
    }
    setInvLoading(true);
    setInvStatus('Validating request...');
    appendInvLog('Validating inputs...');
    try {
      const resources = invResources;
      if (!invRegions.length || !resources.length) {
        throw new Error('Regions and resources are required.');
      }
      startInvProgress(invRegions, resources.length);
      setInvStatus('Running inventory task...');
      appendInvLog(`Submitting request for ${invRegions.length} region(s) and ${resources.length} resource types...`);
      const res = await postJson('/api/tasks/run/', {
        task_id: 'aws_inventory',
        data: {
          access_key: awsAccess.trim(),
          secret_key: awsSecret.trim(),
          regions: invRegions.join(','),
          resources,
          from_date: invFrom.trim(),
          to_date: invTo.trim(),
        },
      });
      setInvLogs((prev) => mergeBackendLogs(prev, res.logs));
      setInvArtifacts(createDownloadEntries(res.artifacts || []));
      setInvStatus(`Completed at ${new Date().toLocaleTimeString()}`);
      appendInvLog('Inventory task completed. Logs refreshed from backend.');
    } catch (err) {
      setInvError(err.message || String(err));
      setInvStatus('Failed to run inventory.');
      appendInvLog(`Inventory failed: ${err.message || err}`);
      if (err.logs) {
        setInvLogs((prev) => mergeBackendLogs(prev, err.logs));
      }
    } finally {
      setInvLoading(false);
      stopInvProgress();
    }
  };

  const runSecurityAudit = async () => {
    setAuditError('');
    setAuditLogs('');
    setAuditArtifacts([]);
    if (!awsAccess.trim() || !awsSecret.trim()) {
      setAuditError('AWS access key and secret are required.');
      return;
    }
    setAuditLoading(true);
    try {
      const res = await postJson('/api/tasks/run/', {
        task_id: 'aws_security_audit',
        data: {
          access_key: awsAccess.trim(),
          secret_key: awsSecret.trim(),
        },
      });
      setAuditLogs((prev) => mergeBackendLogs(prev, res.logs));
      setAuditArtifacts(createDownloadEntries(res.artifacts || []));
    } catch (err) {
      setAuditError(err.message || String(err));
      if (err.logs) {
        setAuditLogs((prev) => mergeBackendLogs(prev, err.logs));
      }
    } finally {
      setAuditLoading(false);
    }
  };

  const runGcpSecurityAudit = async () => {
    setGcpAuditError('');
    setGcpAuditLogs('');
    setGcpAuditArtifacts([]);
    if (!gcpAuditServiceKey.trim()) {
      setGcpAuditError('Upload a GCP service account JSON key.');
      return;
    }
    if (!selectedGcpAuditProjects.length) {
      setGcpAuditError('Select at least one GCP project.');
      return;
    }
    setGcpAuditLoading(true);
    try {
      const res = await postJson('/api/tasks/run/', {
        task_id: 'gcp_security_audit',
        data: {
          gcp_service_key: gcpAuditServiceKey,
          projects: selectedGcpAuditProjects,
        },
      });
      setGcpAuditLogs((prev) => mergeBackendLogs(prev, res.logs));
      setGcpAuditArtifacts(createDownloadEntries(res.artifacts || []));
    } catch (err) {
      setGcpAuditError(err.message || String(err));
      if (err.logs) {
        setGcpAuditLogs((prev) => mergeBackendLogs(prev, err.logs));
      }
    } finally {
      setGcpAuditLoading(false);
    }
  };

  const runTcoReport = async () => {
    setTcoError('');
    setTcoLogs('');
    setTcoArtifacts([]);
    if (!tcoCsvContent.trim()) {
      setTcoError('Upload an AWS billing CSV file.');
      return;
    }
    setTcoLoading(true);
    try {
      const res = await postJson('/api/tasks/run/', {
        task_id: 'tco_report',
        data: {
          csv_content: tcoCsvContent,
          filename: tcoFileName,
        },
      });
      setTcoLogs((prev) => mergeBackendLogs(prev, res.logs));
      setTcoArtifacts(createDownloadEntries(res.artifacts || []));
    } catch (err) {
      setTcoError(err.message || String(err));
      if (err.logs) {
        setTcoLogs((prev) => mergeBackendLogs(prev, err.logs));
      }
    } finally {
      setTcoLoading(false);
    }
  };

  const toggleInventoryRegion = (regionId) => {
    setInvRegions((prev) =>
      prev.includes(regionId) ? prev.filter((id) => id !== regionId) : [...prev, regionId]
    );
  };

  const setAllRegions = () => {
    setInvRegions(AWS_REGION_CHOICES.map((region) => region.id));
  };

  const clearRegions = () => {
    setInvRegions([]);
  };

  const toggleInventoryResource = (resourceId) => {
    setInvResources((prev) =>
      prev.includes(resourceId) ? prev.filter((id) => id !== resourceId) : [...prev, resourceId]
    );
  };

  const selectAllResources = () => {
    setInvResources([...INVENTORY_RESOURCE_IDS]);
  };

  const clearResources = () => {
    setInvResources([]);
  };

  const toggleAwsSubnetSelection = (subnetId) => {
    setSelectedAwsSubnets((prev) =>
      prev.includes(subnetId) ? prev.filter((id) => id !== subnetId) : [...prev, subnetId]
    );
  };

  const selectAllAwsSubnets = () => {
    setSelectedAwsSubnets(subnetRows.map((subnet) => subnet.id));
  };

  const clearAwsSubnets = () => {
    setSelectedAwsSubnets([]);
  };

  const minAsn = 64512;
  const maxAsn = 65534;
  const clampAsn = (value, fallback = minAsn) => {
    const num = parseInt(value, 10);
    if (Number.isNaN(num)) return fallback;
    if (num < minAsn) return minAsn;
    if (num > maxAsn) return maxAsn;
    return num;
  };
  const ensureDifferentAsn = (awsAsn, gcpAsn) => {
    const awsVal = clampAsn(awsAsn);
    let gcpVal = clampAsn(gcpAsn);
    if (awsVal === gcpVal) {
      gcpVal = awsVal + 1 <= maxAsn ? awsVal + 1 : awsVal - 1;
    }
    return { awsVal, gcpVal };
  };

  const handleHaAwsAsnBlur = () => {
    const clampedAws = clampAsn(haAwsAsn, haAwsAsn);
    const clampedGcp = clampAsn(haGcpAsn, haGcpAsn);
    setHaAwsAsn(String(clampedAws));
    if (clampedAws !== parseInt(haAwsAsn, 10)) {
      setHaAsnError(`AWS ASN adjusted to ${clampedAws} (allowed range: ${minAsn}-${maxAsn}).`);
    } else if (clampedAws === clampedGcp) {
      setHaAsnError(`AWS ASN and GCP ASN cannot match. Allowed range: ${minAsn}-${maxAsn}.`);
    } else {
      setHaAsnError('');
    }
  };

  const handleHaGcpAsnBlur = () => {
    const clamped = clampAsn(haGcpAsn, haGcpAsn);
    setHaGcpAsn(String(clamped));
    if (clamped !== parseInt(haGcpAsn, 10)) {
      setHaAsnError(`GCP ASN adjusted to ${clamped} (allowed range: ${minAsn}-${maxAsn}).`);
    } else if (clamped === clampAsn(haAwsAsn)) {
      setHaAsnError(`AWS ASN and GCP ASN cannot match. Allowed range: ${minAsn}-${maxAsn}.`);
    } else {
      setHaAsnError('');
    }
  };

  const handleClassicAwsAsnBlur = () => {
    const clampedAws = clampAsn(classicAwsAsn, classicAwsAsn);
    const clampedGcp = clampAsn(classicGcpAsn, classicGcpAsn);
    setClassicAwsAsn(String(clampedAws));
    if (clampedAws !== parseInt(classicAwsAsn, 10)) {
      setClassicAsnError(`AWS ASN adjusted to ${clampedAws} (allowed range: ${minAsn}-${maxAsn}).`);
    } else if (clampedAws === clampedGcp) {
      setClassicAsnError(`AWS ASN and GCP ASN cannot match. Allowed range: ${minAsn}-${maxAsn}.`);
    } else {
      setClassicAsnError('');
    }
  };

  const handleClassicGcpAsnBlur = () => {
    const clamped = clampAsn(classicGcpAsn, classicGcpAsn);
    setClassicGcpAsn(String(clamped));
    if (clamped !== parseInt(classicGcpAsn, 10)) {
      setClassicAsnError(`GCP ASN adjusted to ${clamped} (allowed range: ${minAsn}-${maxAsn}).`);
    } else if (clamped === clampAsn(classicAwsAsn)) {
      setClassicAsnError(`AWS ASN and GCP ASN cannot match. Allowed range: ${minAsn}-${maxAsn}.`);
    } else {
      setClassicAsnError('');
    }
  };

  const toggleGcpSubnetSelection = (name) => {
    setSelectedGcpSubnets((prev) =>
      prev.includes(name) ? prev.filter((id) => id !== name) : [...prev, name]
    );
  };

  const selectAllGcpSubnets = () => {
    setSelectedGcpSubnets(gcpSubnetOptions.map((subnet) => subnet.name));
  };

  const clearGcpSubnets = () => {
    setSelectedGcpSubnets([]);
  };

  const toggleGcpAuditProject = (projectId) => {
    setSelectedGcpAuditProjects((prev) =>
      prev.includes(projectId) ? prev.filter((id) => id !== projectId) : [...prev, projectId]
    );
  };

  const selectAllGcpAuditProjects = () => {
    setSelectedGcpAuditProjects(gcpAuditProjects.map((project) => project.project_id));
  };

  const clearGcpAuditProjects = () => {
    setSelectedGcpAuditProjects([]);
  };

  const currentInvLogText = invLogs || (invLoading ? 'Inventory is running, waiting for server logs...' : 'Logs will appear here once a run starts.');

  const cardVariants = {
    hidden: { opacity: 0, y: 20, scale: 0.95 },
    visible: (i) => ({
      opacity: 1,
      y: 0,
      scale: 1,
      transition: {
        delay: i * 0.1,
        duration: 0.5,
        ease: "easeOut"
      }
    }),
    hover: {
      y: -8,
      scale: 1.02,
      transition: {
        duration: 0.3,
        ease: "easeOut"
      }
    }
  };

  const containerVariants = {
    hidden: { opacity: 0 },
    visible: {
      opacity: 1,
      transition: {
        staggerChildren: 0.1
      }
    }
  };

  return (
    <div className="dashboard-container">
      <motion.h1
        initial={{ opacity: 0, y: -20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6 }}
      >
        Lens Backend Demo
      </motion.h1>
      
      {/* Dark Mode Toggle */}
      <button
        onClick={() => setDarkMode(!darkMode)}
        style={{
          position: 'fixed',
          top: '20px',
          right: '20px',
          width: '50px',
          height: '50px',
          borderRadius: '50%',
          border: 'none',
          background: darkMode ? '#1e293b' : '#ffffff',
          color: darkMode ? '#fbbf24' : '#1e293b',
          fontSize: '24px',
          cursor: 'pointer',
          boxShadow: '0 4px 12px rgba(0, 0, 0, 0.15)',
          zIndex: 100,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          transition: 'all 0.3s ease',
          pointerEvents: 'auto',
        }}
        title={darkMode ? 'Switch to Light Mode' : 'Switch to Dark Mode'}
      >
        {darkMode ? '☀️' : '🌙'}
      </button>
      <motion.p
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 0.2, duration: 0.6 }}
        style={{ marginBottom: '2rem', color: '#64748b', fontSize: '1.1rem' }}
      >
        Select an automation task to get started.
      </motion.p>

      {view === 'home' && (
      <motion.div
        className="card-grid"
        variants={containerVariants}
        initial="hidden"
        animate="visible"
      >
        <motion.div
          className="task-card"
          variants={cardVariants}
          custom={0}
          whileHover="hover"
          whileTap={{ scale: 0.98 }}
        >
          <h2>VPC Terraform Toolkit</h2>
          <p>Convert AWS VPCs into GCP VPCs ready Terraform bundles with per-subnet overrides.</p>
          <button onClick={() => setView('terraform')}>Open Toolkit</button>
          </motion.div>
        <motion.div
          className="task-card"
          variants={cardVariants}
          custom={1}
          whileHover="hover"
          whileTap={{ scale: 0.98 }}
        >
          <h2>AWS Inventory Export</h2>
          <p>Create XLSX-based resource inventories directly from your browser.</p>
          <button onClick={() => setView('inventory')}>Run Inventory</button>
        </motion.div>
        <motion.div
          className="task-card"
          variants={cardVariants}
          custom={2}
          whileHover="hover"
          whileTap={{ scale: 0.98 }}
        >
          <h2>AWS Standard Security Audit</h2>
          <p>Generate the standard security audit workbook with the same XLSX format and colors.</p>
          <button onClick={() => setView('security_audit')}>Run Audit</button>
        </motion.div>
        <motion.div
          className="task-card"
          variants={cardVariants}
          custom={3}
          whileHover="hover"
          whileTap={{ scale: 0.98 }}
        >
          <h2>GCP Security Audit</h2>
          <p>Generate a GCP security audit workbook from your service account JSON key.</p>
          <button onClick={() => setView('gcp_security_audit')}>Run Audit</button>
        </motion.div>
        <motion.div
          className="task-card"
          variants={cardVariants}
          custom={4}
          whileHover="hover"
          whileTap={{ scale: 0.98 }}
        >
          <h2>AWS TCO Report</h2>
          <p>Upload an AWS billing CSV and generate the TCO XLSX summary.</p>
          <button onClick={() => setView('tco_report')}>Run TCO</button>
        </motion.div>
        <motion.div
          className="task-card"
          variants={cardVariants}
          custom={5}
          whileHover="hover"
          whileTap={{ scale: 0.98 }}
        >
          <h2>HA VPN Builder</h2>
          <p>Design a redundant AWS &lt;-&gt; GCP HA VPN with dual tunnels and BGP routing.</p>
          <button onClick={() => setView('ha_vpn')}>Plan HA VPN</button>
        </motion.div>
        <motion.div
          className="task-card"
          variants={cardVariants}
          custom={6}
          whileHover="hover"
          whileTap={{ scale: 0.98 }}
        >
          <h2>Classic VPN Builder</h2>
          <p>Provision single-tunnel AWS &lt;-&gt; GCP Classic VPN with BGP and IKE version selection.</p>
          <button onClick={() => setView('classic_vpn')}>Plan Classic VPN</button>
        </motion.div>
        <motion.div
          className="task-card"
          variants={cardVariants}
          custom={7}
          whileHover="hover"
          whileTap={{ scale: 0.98 }}
        >
          <h2>ECR to Artifact Registry</h2>
          <p>Migrate all ECR repos to GCP Artifact Registry with parallel pushes and skip existing tags.</p>
          <button onClick={() => setView('ecr_migration')}>Migrate Repos</button>
          </motion.div>
        <motion.div
          className="task-card"
          variants={cardVariants}
          custom={8}
          whileHover="hover"
          whileTap={{ scale: 0.98 }}
        >
          <h2>ECS → GKE Terraform</h2>
          <p>Size a regional GKE cluster from ECS services and download Terraform bundles.</p>
          <button onClick={() => setView('ecs_terraform')}>Plan Cluster</button>
        </motion.div>
        <motion.div
          className="task-card"
          variants={cardVariants}
          custom={9}
          whileHover="hover"
          whileTap={{ scale: 0.98 }}
        >
          <h2>{'EKS -> GKE Terraform'}</h2>
          <p>Size a GKE cluster directly from existing EKS nodegroups and download Terraform bundles.</p>
          <button onClick={() => setView('eks_terraform')}>Plan EKS Cluster</button>
        </motion.div>
        <motion.div
          className="task-card"
          variants={cardVariants}
          custom={10}
          whileHover="hover"
          whileTap={{ scale: 0.98 }}
        >
          <h2>ECS → GKE Manifests</h2>
          <p>Convert ECS task definitions into curated Kubernetes manifests using Gemini.</p>
          <button onClick={() => setView('ecs_manifests')}>Generate Manifests</button>
        </motion.div>
        <motion.div
          className="task-card"
          variants={cardVariants}
          custom={11}
          whileHover="hover"
          whileTap={{ scale: 0.98 }}
        >
          <h2>EKS → GKE Manifests</h2>
          <p>Export live EKS workloads and clean them for GKE without Gemini.</p>
          <button onClick={() => setView('eks_manifests')}>Export Manifests</button>
        </motion.div>
        <motion.div
          className="task-card"
          variants={cardVariants}
          custom={12}
          whileHover="hover"
          whileTap={{ scale: 0.98 }}
        >
          <h2>VM → GKE Manifests</h2>
          <p>Convert VM instances (EC2 or GCP Compute Engine) into Kubernetes manifests using Gemini.</p>
          <button onClick={() => setView('vm2gke_manifests')}>Generate Manifests</button>
        </motion.div>
        <motion.div
          className="task-card"
          variants={cardVariants}
          custom={13}
          whileHover="hover"
          whileTap={{ scale: 0.98 }}
        >
          <h2>Box AWS Terraform Generator</h2>
          <p>Generate Terraform modules for AWS services using boto3 to fetch real AWS data (AMIs, instance types, etc.).</p>
          <button onClick={() => setView('box_project_aws')}>Build AWS Project</button>
        </motion.div>
        <motion.div
          className="task-card"
          variants={cardVariants}
          custom={14}
          whileHover="hover"
          whileTap={{ scale: 0.98 }}
        >
          <h2>DocumentDB to Atlas Migration</h2>
          <p>Migrate data from AWS DocumentDB to MongoDB Atlas with support for fresh and incremental sync modes.</p>
          <button onClick={() => setView('docdb_migration')}>Start Migration</button>
        </motion.div>
        </motion.div>
      )}

      {view !== 'home' && (
        <motion.div
          className="view-nav"
          initial={{ opacity: 0, x: -20 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ duration: 0.4 }}
        >
          <motion.button
            onClick={() => setView('home')}
            whileHover={{ scale: 1.05, x: -2 }}
            whileTap={{ scale: 0.98 }}
          >
            ← Back to task list
          </motion.button>
        </motion.div>
      )}

      {(['terraform', 'inventory', 'security_audit', 'ha_vpn', 'classic_vpn', 'ecr_migration', 'ecs_terraform', 'eks_terraform', 'ecs_manifests', 'eks_manifests'].includes(view)) && (
      <motion.fieldset
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
      >
        <legend>AWS Credentials & Region</legend>
        <label>
          AWS Access Key ID
          <input value={awsAccess} onChange={(e) => setAwsAccess(e.target.value)} placeholder="AKIA..." />
        </label>
        <label>
          AWS Secret Access Key
          <input type="password" value={awsSecret} onChange={(e) => setAwsSecret(e.target.value)} placeholder="••••" />
        </label>
        {['terraform', 'ha_vpn', 'classic_vpn', 'ecr_migration', 'ecs_terraform', 'eks_terraform', 'ecs_manifests', 'eks_manifests'].includes(view) && (
          <>
            <label>
              AWS Region
              <select value={awsRegion} onChange={(e) => setAwsRegion(e.target.value)}>
                {AWS_REGIONS.map((region) => (
                  <option key={region.id} value={region.id}>
                    {getAwsRegionDisplay(region)}
                  </option>
                ))}
              </select>
            </label>
            {awsRegion === 'custom' && (
              <label>
                Custom Region
                <input value={customRegion} onChange={(e) => setCustomRegion(e.target.value)} placeholder="e.g. us-gov-west-1" />
              </label>
            )}
          </>
        )}
        {['terraform', 'ha_vpn', 'classic_vpn'].includes(view) && (
          <>
            <small>VPCs load automatically when all fields above are populated.</small>
            <label>
              VPC
              <select value={selectedVpc} onChange={(e) => setSelectedVpc(e.target.value)}>
                <option value="">-- Select VPC --</option>
                {vpcs.map((vpc) => {
                  const labelName = vpc.name || vpc.id;
                  const optionLabel = `${labelName} - ${vpc.id} - ${vpc.cidr || ''}`.replace(/\s+-\s+$/, '');
                  return (
                    <option key={vpc.id} value={vpc.id}>
                      {optionLabel}
                    </option>
                  );
                })}
              </select>
            </label>
            {view === 'terraform' && <small>Subnets load automatically once a VPC is chosen.</small>}
            {vpcError && <div className="error">{vpcError}</div>}
            {['terraform', 'classic_vpn', 'ha_vpn'].includes(view) && subnetError && <div className="error">{subnetError}</div>}
          </>
        )}
        {showAwsSubnetSelector && (
          <div className="aws-subnet-selector">
            <div className="label-row">
              <label>AWS Subnets</label>
              <div className="pill-actions">
                <button type="button" onClick={selectAllAwsSubnets} disabled={!subnetRows.length}>
                  Select all
                </button>
                <button type="button" onClick={clearAwsSubnets} disabled={!selectedAwsSubnets.length}>
                  Clear
                </button>
              </div>
            </div>
            {selectedVpc && (
              <small className="info-callout">VPC ID: {selectedVpc}</small>
            )}
            <small>
              Selected subnets determine which route tables receive VGW propagation. Leave everything selected to cover the entire VPC, or clear all to skip propagation.
            </small>
            <div className="checkbox-grid">
              {subnetRows.map((subnet) => (
                <label key={subnet.id} className="checkbox-item">
                  <input
                    type="checkbox"
                    checked={selectedAwsSubnets.includes(subnet.id)}
                    onChange={() => toggleAwsSubnetSelection(subnet.id)}
                  />
                  <span className="subnet-label">
                    <strong>{subnet.name || subnet.id}</strong>
                    <small>{subnet.id}</small>
                    <small>({subnet.cidr || 'unknown'})</small>
                  </span>
                </label>
              ))}
            </div>
          </div>
        )}
        {view === 'terraform' && subnetRows.length > 0 && (
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Subnet ID</th>
                  <th>AZ</th>
                  <th>Name Override</th>
                  <th>CIDR Override</th>
                  <th>Suggested Name</th>
                </tr>
              </thead>
              <tbody>
                {subnetRows.map((subnet, idx) => (
                  <tr key={subnet.id}>
                    <td>{subnet.id}</td>
                    <td>{subnet.az}</td>
                    <td>
                      <input
                        value={subnet.overrideName}
                        onChange={(e) => handleSubnetChange(idx, 'overrideName', e.target.value)}
                      />
                    </td>
                    <td>
                      <input
                        value={subnet.overrideCidr}
                        onChange={(e) => handleSubnetChange(idx, 'overrideCidr', e.target.value)}
                      />
                    </td>
                    <td>{subnet.suggested_name}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </motion.fieldset>
      )}

      {view === 'terraform' && (
      <motion.fieldset
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
      >
        <legend>GCP Target</legend>
        <label>
          GCP Project ID
          <input value={gcpProject} onChange={(e) => setGcpProject(e.target.value)} placeholder="my-gcp-project" />
        </label>
        <label>
          GCP VPC Network Name
          <input value={gcpNetwork} onChange={(e) => setGcpNetwork(e.target.value)} placeholder="aws-migration" />
        </label>
        <label>
          GCP Region
          <select value={gcpRegion} onChange={(e) => setGcpRegion(e.target.value)}>
            {GCP_REGIONS.map((region) => (
              <option key={region.id} value={region.id}>
                {region.display}
              </option>
            ))}
          </select>
        </label>
      </motion.fieldset>
      )}

      {view === 'terraform' && (
      <motion.fieldset
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
      >
        <legend>Terraform Bundle</legend>
        <button onClick={runTerraformTask} disabled={!selectedVpc || !authReady}>Generate Terraform Bundle</button>
        {tfError && <div className="error">{tfError}</div>}
        <h3>Logs</h3>
        <pre>{tfLogs}</pre>
        <h3>Artifacts</h3>
        <div className="artifacts">
          {tfArtifacts.map((artifact) => (
            <a className="download-link" key={artifact.url} href={artifact.url} download={artifact.filename}>
              Download {artifact.filename}
            </a>
          ))}
        </div>
      </motion.fieldset>
      )}

      {view === 'ecs_terraform' && (
      <>
      <fieldset>
        <legend>ECS Cluster & Target</legend>
        <label>
          ECS Cluster
          <select
            value={ecsClusterSelectValue}
            onChange={(e) => setEcsClusterName(e.target.value)}
            disabled={!ecsClusters.length}
          >
            <option value="">-- Select cluster --</option>
            {ecsClusters.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        {ecsClusterLoading && <small>Loading ECS clusters...</small>}
        {ecsClusterError && <div className="error">{ecsClusterError}</div>}
        <label>
          Cluster Name (manual override)
          <input
            value={ecsClusterName}
            onChange={(e) => setEcsClusterName(e.target.value)}
            placeholder="my-ecs-cluster"
          />
        </label>
        <small>Clusters refresh automatically when you change the AWS region.</small>
        <label>
          GCP Project ID
          <input value={ecsTfGcpProject} onChange={(e) => setEcsTfGcpProject(e.target.value)} placeholder="my-gcp-project" />
        </label>
        <label>
          GCP Location
          <select value={ecsTfGcpLocation} onChange={(e) => setEcsTfGcpLocation(e.target.value)}>
            {GCP_REGIONS.map((region) => (
              <option key={region.id} value={region.id}>
                {region.display}
              </option>
            ))}
          </select>
        </label>
        <label>
          GKE Cluster Name (optional)
          <input value={ecsTfGkeName} onChange={(e) => setEcsTfGkeName(e.target.value)} placeholder={`${ecsClusterName || 'cluster'}-gke`} />
        </label>
        <label>
          GCP VPC Network (optional)
          <input value={ecsTfNetwork} onChange={(e) => setEcsTfNetwork(e.target.value)} placeholder="shared-vpc" />
        </label>
        <label>
          GCP Subnetwork (optional)
          <input value={ecsTfSubnetwork} onChange={(e) => setEcsTfSubnetwork(e.target.value)} placeholder="gke-subnet" />
        </label>
      </fieldset>
      <fieldset>
        <legend>Node & Node Pool Sizing</legend>
        <label>
          Node Machine Type
          <input value={ecsTfMachineType} onChange={(e) => setEcsTfMachineType(e.target.value)} placeholder="e2-standard-4" />
        </label>
        <label>
          Node CPU (vCPU, optional)
          <input value={ecsTfNodeCpu} onChange={(e) => setEcsTfNodeCpu(e.target.value)} placeholder="4" />
        </label>
        <label>
          Node Memory (MB, optional)
          <input value={ecsTfNodeMem} onChange={(e) => setEcsTfNodeMem(e.target.value)} placeholder="16384" />
        </label>
        <label>
          Min Nodes
          <input value={ecsTfMinNodes} onChange={(e) => setEcsTfMinNodes(e.target.value)} placeholder="3" />
        </label>
        <label>
          Max Nodes
          <input value={ecsTfMaxNodes} onChange={(e) => setEcsTfMaxNodes(e.target.value)} placeholder="6" />
        </label>
        <label>
          Node Locations (comma-separated, optional)
          <input value={ecsTfNodeLocations} onChange={(e) => setEcsTfNodeLocations(e.target.value)} placeholder="us-central1-a,us-central1-b" />
        </label>
        <label>
          Node Pool Name
          <input value={ecsTfNodePoolName} onChange={(e) => setEcsTfNodePoolName(e.target.value)} placeholder="primary" />
        </label>
        <label>
          Node Pool Subnetwork (optional)
          <input value={ecsTfNodePoolSubnet} onChange={(e) => setEcsTfNodePoolSubnet(e.target.value)} placeholder="gke-subnet" />
        </label>
        <label>
          Node Pool Zones (comma-separated, optional)
          <input value={ecsTfNodePoolZones} onChange={(e) => setEcsTfNodePoolZones(e.target.value)} placeholder="us-central1-a" />
        </label>
        <label>
          Node Service Account (optional)
          <input value={ecsTfServiceAccount} onChange={(e) => setEcsTfServiceAccount(e.target.value)} placeholder="gke-nodes@project.iam.gserviceaccount.com" />
        </label>
        <label>
          Release Channel
          <input value={ecsTfReleaseChannel} onChange={(e) => setEcsTfReleaseChannel(e.target.value)} placeholder="REGULAR" />
        </label>
        <label>
          Master IPv4 CIDR (/28, optional)
          <input value={ecsTfMasterCidr} onChange={(e) => setEcsTfMasterCidr(e.target.value)} placeholder="172.16.0.0/28" />
        </label>
        <label className="checkbox-row">
          <input
            type="checkbox"
            checked={ecsTfPrivateNodes}
            onChange={(e) => {
              const checked = e.target.checked;
              setEcsTfPrivateNodes(checked);
              if (!checked) {
                setEcsTfPrivateEndpoint(false);
              }
            }}
          />
          <span>Use private nodes</span>
        </label>
        <label className="checkbox-row">
          <input
            type="checkbox"
            checked={ecsTfPrivateEndpoint}
            onChange={(e) => setEcsTfPrivateEndpoint(e.target.checked)}
            disabled={!ecsTfPrivateNodes}
          />
          <span>Restrict control plane to private endpoint</span>
        </label>
      </fieldset>
      <fieldset>
        <legend>ECS Services & Output</legend>
        <div className="aws-subnet-selector">
          <label>ECS Services</label>
          {ecsServicesLoading && <small>Loading ECS services...</small>}
          {ecsServicesError && <div className="error">{ecsServicesError}</div>}
          {!ecsServices.length && !ecsServicesLoading && <small>No ECS services detected for this cluster.</small>}
          {!!ecsServices.length && !ecsServicesLoading && (
            <small>
              All detected ECS services ({ecsServices.length}) will be included automatically when generating the Terraform bundle.
            </small>
          )}
        </div>
        <div className="info-callout">
          Generates Terraform bundles sized to your ECS footprint. AWS CLI and Terraform CLIs must be installed on the backend host.
        </div>
        <button onClick={runEcsTerraformTask} disabled={!authReady || !ecsClusterName || !ecsTfGcpProject.trim()}>
          Generate ECS Terraform Bundle
        </button>
        {ecsTfError && <div className="error">{ecsTfError}</div>}
        <h3>Logs</h3>
        <pre>{ecsTfLogs || 'Logs will appear here once a run starts.'}</pre>
        <h3>Artifacts</h3>
        <div className="artifacts">
          {ecsTfArtifacts.map((artifact) => (
            <a className="download-link" key={artifact.url} href={artifact.url} download={artifact.filename}>
              Download {artifact.filename}
            </a>
          ))}
        </div>
      </fieldset>
      </>
      )}

      {view === 'eks_terraform' && (
        <>
          <fieldset>
            <legend>EKS Cluster & Target</legend>
            <label>
              EKS Cluster
              <select value={eksClusterSelectValue} onChange={(e) => setEksClusterName(e.target.value)} disabled={!eksClusters.length}>
                <option value="">-- Select cluster --</option>
                {eksClusters.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            {eksClusterLoading && <small>Loading EKS clusters...</small>}
            {eksClusterError && <div className="error">{eksClusterError}</div>}
            <label>
              GCP Project ID
              <input value={eksTfGcpProject} onChange={(e) => setEksTfGcpProject(e.target.value)} placeholder="my-gcp-project" />
            </label>
            <label>
              GCP Location
              <select value={eksTfGcpLocation} onChange={(e) => setEksTfGcpLocation(e.target.value)}>
                {GCP_REGIONS.map((region) => (
                  <option key={region.id} value={region.id}>
                    {region.display}
                  </option>
                ))}
              </select>
            </label>
            <label>
              GKE Cluster Name (optional)
              <input value={eksTfGkeName} onChange={(e) => setEksTfGkeName(e.target.value)} placeholder={`${eksClusterName || 'cluster'}-gke`} />
            </label>
            <label>
              GCP VPC Network (optional)
              <input value={eksTfNetwork} onChange={(e) => setEksTfNetwork(e.target.value)} placeholder="shared-vpc" />
            </label>
            <label>
              GCP Subnetwork (optional)
              <input value={eksTfSubnetwork} onChange={(e) => setEksTfSubnetwork(e.target.value)} placeholder="gke-subnet" />
            </label>
          </fieldset>
          <fieldset>
            <legend>Node Sizing & Controls</legend>
            <label>
              Node Machine Type
              <input value={eksTfMachineType} onChange={(e) => setEksTfMachineType(e.target.value)} placeholder="e2-standard-4" />
            </label>
            <label>
              Node CPU (vCPU, optional)
              <input value={eksTfNodeCpu} onChange={(e) => setEksTfNodeCpu(e.target.value)} placeholder="4" />
            </label>
            <label>
              Node Memory (MB, optional)
              <input value={eksTfNodeMem} onChange={(e) => setEksTfNodeMem(e.target.value)} placeholder="16384" />
            </label>
            <label>
              Min Nodes
              <input value={eksTfMinNodes} onChange={(e) => setEksTfMinNodes(e.target.value)} placeholder="3" />
            </label>
            <label>
              Max Nodes
              <input value={eksTfMaxNodes} onChange={(e) => setEksTfMaxNodes(e.target.value)} placeholder="6" />
            </label>
            <label>
              Node Locations (comma-separated, optional)
              <input value={eksTfNodeLocations} onChange={(e) => setEksTfNodeLocations(e.target.value)} placeholder="us-central1-a,us-central1-b" />
            </label>
            <label>
              Node Service Account (optional)
              <input value={eksTfServiceAccount} onChange={(e) => setEksTfServiceAccount(e.target.value)} placeholder="gke-nodes@project.iam.gserviceaccount.com" />
            </label>
            <label>
              Release Channel
              <input value={eksTfReleaseChannel} onChange={(e) => setEksTfReleaseChannel(e.target.value)} placeholder="REGULAR" />
            </label>
            <label>
              Master IPv4 CIDR (/28, optional)
              <input value={eksTfMasterCidr} onChange={(e) => setEksTfMasterCidr(e.target.value)} placeholder="172.16.0.0/28" />
            </label>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={eksTfPrivateNodes}
                onChange={(e) => {
                  const checked = e.target.checked;
                  setEksTfPrivateNodes(checked);
                  if (!checked) {
                    setEksTfPrivateEndpoint(false);
                  }
                }}
              />
              <span>Use private nodes</span>
            </label>
            <label className="checkbox-row">
              <input type="checkbox" checked={eksTfPrivateEndpoint} onChange={(e) => setEksTfPrivateEndpoint(e.target.checked)} disabled={!eksTfPrivateNodes} />
              <span>Restrict control plane to private endpoint</span>
            </label>
          </fieldset>
          <fieldset>
            <legend>Terraform Bundle</legend>
            <div className="info-callout">
              Always runs <code>terraform init</code> and <code>terraform validate</code>; ensure Terraform CLI is installed on the backend host.
            </div>
            <button onClick={runEksTerraformTask} disabled={!authReady || !eksClusterName || !eksTfGcpProject.trim()}>
              Generate EKS Terraform Bundle
            </button>
            {eksTfError && <div className="error">{eksTfError}</div>}
            <h3>Logs</h3>
            <pre>{eksTfLogs || 'Logs will appear here once a run starts.'}</pre>
            <h3>Artifacts</h3>
            <div className="artifacts">
              {eksTfArtifacts.map((artifact) => (
                <a className="download-link" key={artifact.url} href={artifact.url} download={artifact.filename}>
                  Download {artifact.filename}
                </a>
              ))}
            </div>
          </fieldset>
        </>
      )}

      {view === 'ecs_manifests' && (
      <>
      <fieldset>
        <legend>ECS Cluster & Services</legend>
        <label>
          ECS Cluster
          <select
            value={ecsClusterSelectValue}
            onChange={(e) => setEcsClusterName(e.target.value)}
            disabled={!ecsClusters.length}
          >
            <option value="">-- Select cluster --</option>
            {ecsClusters.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        {ecsClusterLoading && <small>Loading ECS clusters...</small>}
        {ecsClusterError && <div className="error">{ecsClusterError}</div>}
        <label>
          Cluster Name (manual override)
          <input
            value={ecsClusterName}
            onChange={(e) => setEcsClusterName(e.target.value)}
            placeholder="my-ecs-cluster"
          />
        </label>
        <div className="aws-subnet-selector">
          <div className="label-row">
            <label>Services to convert</label>
            <div className="pill-actions">
              <button type="button" onClick={selectAllManifestServices} disabled={!ecsServices.length}>
                Select all
              </button>
              <button type="button" onClick={clearManifestServices} disabled={!ecsManifestServices.length}>
                Clear
              </button>
            </div>
          </div>
          {ecsServicesLoading && <small>Loading ECS services...</small>}
          {ecsServicesError && <div className="error">{ecsServicesError}</div>}
          <div className="checkbox-grid">
            {ecsServices.map((service) => (
              <label key={service} className="checkbox-item">
                <input
                  type="checkbox"
                  checked={ecsManifestServices.includes(service)}
                  onChange={() => toggleManifestService(service)}
                />
                <span>{service}</span>
              </label>
            ))}
          </div>
        </div>
      </fieldset>
      <fieldset>
        <legend>Manifest Generation</legend>
        <div className="info-callout">
          Gemini model, fallbacks, and credential settings are controlled via the server <code>.env</code>. Update the backend
          configuration to change these defaults.
        </div>
        <button onClick={runEcsManifestTask} disabled={!authReady || !ecsClusterName}>
          Generate Kubernetes Manifests
        </button>
        {ecsManifestError && <div className="error">{ecsManifestError}</div>}
        <h3>Logs</h3>
        <pre>{ecsManifestLogs || 'Logs will appear here once a run starts.'}</pre>
        <h3>Artifacts</h3>
        <div className="artifacts">
          {ecsManifestArtifacts.map((artifact) => (
            <a className="download-link" key={artifact.url} href={artifact.url} download={artifact.filename}>
              Download {artifact.filename}
            </a>
          ))}
        </div>
      </fieldset>
      </>
      )}

      {view === 'eks_manifests' && (
      <>
      <fieldset>
        <legend>EKS Cluster & Namespaces</legend>
        <label>
          EKS Cluster
          <select
            value={eksClusterSelectValue}
            onChange={(e) => setEksClusterName(e.target.value)}
            disabled={!eksClusters.length}
          >
            <option value="">-- Select cluster --</option>
            {eksClusters.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        {eksClusterLoading && <small>Loading EKS clusters...</small>}
        {eksClusterError && <div className="error">{eksClusterError}</div>}
        <div className="aws-subnet-selector">
          <div className="label-row">
            <label>Namespaces to export</label>
            <div className="pill-actions">
              <button type="button" onClick={selectAllEksNamespaces} disabled={!eksNamespaces.length}>
                Select all
              </button>
              <button type="button" onClick={clearEksNamespaces} disabled={!selectedEksNamespaces.length}>
                Clear
              </button>
            </div>
          </div>
          {eksNamespacesLoading && <small>Loading namespaces...</small>}
          {eksNamespacesError && <div className="error">{eksNamespacesError}</div>}
          {!eksNamespacesLoading && !eksNamespaces.length && (
            <small>No namespaces detected. System namespaces are excluded automatically.</small>
          )}
          <div className="checkbox-grid">
            {eksNamespaces.map((namespace) => (
              <label key={namespace} className="checkbox-item">
                <input
                  type="checkbox"
                  checked={selectedEksNamespaces.includes(namespace)}
                  onChange={() => toggleEksNamespace(namespace)}
                />
                <span>{namespace}</span>
              </label>
            ))}
          </div>
        </div>
        <div className="aws-subnet-selector">
          <div className="label-row">
            <label>Resource Types</label>
            <div className="pill-actions">
              <button type="button" onClick={selectAllEksResourceTypes} disabled={!DEFAULT_EKS_RESOURCE_TYPES.length}>
                Select all
              </button>
              <button type="button" onClick={clearEksResourceTypes} disabled={!selectedEksResourceTypes.length}>
                Clear
              </button>
            </div>
          </div>
          <small>Defaults mirror the helper script; deselect any workloads you do not want exported.</small>
          <div className="checkbox-grid">
            {DEFAULT_EKS_RESOURCE_TYPES.map((resource) => (
              <label key={resource} className="checkbox-item">
                <input
                  type="checkbox"
                  checked={selectedEksResourceTypes.includes(resource)}
                  onChange={() => toggleEksResourceType(resource)}
                />
                <span>{resource}</span>
              </label>
            ))}
          </div>
        </div>
      </fieldset>
      <fieldset>
        <legend>Manifest Export</legend>
        <div className="info-callout">
          Runs the local <code>eks2gke-manifest-local.py</code> helper. Ensure AWS CLI and kubectl are available on the backend host.
          System namespaces are filtered automatically.
        </div>
        <button onClick={runEksManifestTask} disabled={!authReady || !eksClusterName || !selectedEksNamespaces.length}>
          Export EKS Manifests
        </button>
        {eksManifestError && <div className="error">{eksManifestError}</div>}
        <h3>Logs</h3>
        <pre>{eksManifestLogs || 'Logs will appear here once a run starts.'}</pre>
        <h3>Artifacts</h3>
        <div className="artifacts">
          {eksManifestArtifacts.map((artifact) => (
            <a className="download-link" key={artifact.url} href={artifact.url} download={artifact.filename}>
              Download {artifact.filename}
            </a>
          ))}
        </div>
      </fieldset>
      </>
      )}

      {view === 'vm2gke_manifests' && (
      <>
      <fieldset>
        <legend>Cloud Provider & Configuration</legend>
        <div style={{ marginBottom: '1.5rem' }}>
          <label style={{ display: 'block', marginBottom: '0.5rem', fontWeight: 600 }}>
            Cloud Provider
          </label>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '1rem',
              padding: '0.5rem',
              background: '#f1f5f9',
              borderRadius: '8px',
              width: 'fit-content'
            }}
          >
            <button
              type="button"
              onClick={() => setVm2gkeProvider('aws')}
              style={{
                padding: '0.5rem 1.5rem',
                borderRadius: '6px',
                border: 'none',
                cursor: 'pointer',
                fontWeight: 600,
                transition: 'all 0.2s',
                background: vm2gkeProvider === 'aws' ? '#2563eb' : 'transparent',
                color: vm2gkeProvider === 'aws' ? 'white' : '#64748b',
                boxShadow: vm2gkeProvider === 'aws' ? '0 2px 4px rgba(37, 99, 235, 0.3)' : 'none'
              }}
            >
              AWS (EC2)
            </button>
            <button
              type="button"
              onClick={() => setVm2gkeProvider('gcp')}
              style={{
                padding: '0.5rem 1.5rem',
                borderRadius: '6px',
                border: 'none',
                cursor: 'pointer',
                fontWeight: 600,
                transition: 'all 0.2s',
                background: vm2gkeProvider === 'gcp' ? '#2563eb' : 'transparent',
                color: vm2gkeProvider === 'gcp' ? 'white' : '#64748b',
                boxShadow: vm2gkeProvider === 'gcp' ? '0 2px 4px rgba(37, 99, 235, 0.3)' : 'none'
              }}
            >
              GCP (Compute Engine)
            </button>
          </div>
        </div>
        {vm2gkeProvider === 'aws' ? (
          <>
            <label>
              AWS Access Key ID
              <input
                type="text"
                value={awsAccess}
                onChange={(e) => setAwsAccess(e.target.value)}
                placeholder="AKIA..."
              />
            </label>
            <label>
              AWS Secret Access Key
              <input
                type="password"
                value={awsSecret}
                onChange={(e) => setAwsSecret(e.target.value)}
                placeholder="••••"
              />
            </label>
            <label>
              AWS Region
              <select value={vm2gkeAwsRegion} onChange={(e) => setVm2gkeAwsRegion(e.target.value)}>
                <option value="">-- Select region --</option>
                {AWS_REGIONS.filter((r) => r.id !== 'custom').map((region) => (
                  <option key={region.id} value={region.id}>
                    {formatRegionDisplay(region.id, region.label)}
                  </option>
                ))}
              </select>
            </label>
          </>
        ) : (
          <>
            <label>
              Upload GCP Service Account JSON
              <input
                type="file"
                accept="application/json,.json"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (!file) {
                    setVm2gkeGcpServiceKey('');
                    setVm2gkeGcpServiceFileName('');
                    setVm2gkeGcpProjectError('');
                    return;
                  }
                  
                  // Check file size (max 1MB)
                  if (file.size > 1024 * 1024) {
                    setVm2gkeGcpProjectError('File is too large. Maximum size is 1MB.');
                    setVm2gkeGcpServiceKey('');
                    setVm2gkeGcpServiceFileName('');
                    return;
                  }
                  
                  // Check if file is empty
                  if (file.size === 0) {
                    setVm2gkeGcpProjectError('File is empty. Please select a valid JSON file.');
                    setVm2gkeGcpServiceKey('');
                    setVm2gkeGcpServiceFileName('');
                    return;
                  }
                  
                  setVm2gkeGcpServiceFileName(file.name);
                  setVm2gkeGcpProjectError('');
                  
                  const reader = new FileReader();
                  reader.onerror = () => {
                    setVm2gkeGcpProjectError('Failed to read file. Please try again.');
                    setVm2gkeGcpServiceKey('');
                    setVm2gkeGcpServiceFileName('');
                  };
                  reader.onload = (evt) => {
                    const content = evt.target?.result?.toString() || '';
                    if (!content.trim()) {
                      setVm2gkeGcpProjectError('File appears to be empty. Please select a valid JSON file.');
                      setVm2gkeGcpServiceKey('');
                      setVm2gkeGcpServiceFileName('');
                      return;
                    }
                    
                    // Validate JSON before setting
                    try {
                      JSON.parse(content);
                      setVm2gkeGcpServiceKey(content);
                      setVm2gkeGcpProjectError('');
                    } catch (err) {
                      setVm2gkeGcpProjectError(`Invalid JSON: ${err.message}`);
                      setVm2gkeGcpServiceKey('');
                      setVm2gkeGcpServiceFileName('');
                    }
                  };
                  reader.readAsText(file);
                }}
              />
            </label>
            {vm2gkeGcpServiceFileName && (
              <small className="file-indicator">
                Loaded: {vm2gkeGcpServiceFileName}
                <button
                  type="button"
                  onClick={() => {
                    setVm2gkeGcpServiceKey('');
                    setVm2gkeGcpServiceFileName('');
                    setVm2gkeGcpProjectOptions([]);
                    setVm2gkeGcpProject('');
                  }}
                >
                  Clear
                </button>
              </small>
            )}
            {vm2gkeGcpProjectsLoading && <small>Loading GCP projects...</small>}
            {vm2gkeGcpProjectError && <div className="error">{vm2gkeGcpProjectError}</div>}
            {vm2gkeGcpProjectOptions.length > 0 ? (
              <label>
                GCP Project
                <select
                  value={vm2gkeGcpProject}
                  onChange={(e) => setVm2gkeGcpProject(e.target.value)}
                >
                  {vm2gkeGcpProjectOptions.map((project) => (
                    <option key={project.project_id || project.name} value={project.project_id}>
                      {project.display_name && project.display_name !== project.project_id
                        ? `${project.display_name} (${project.project_id})`
                        : project.project_id}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              vm2gkeGcpServiceKey.trim() && !vm2gkeGcpProjectsLoading && !vm2gkeGcpProjectError && (
                <div className="info-callout">No GCP projects detected for this service account.</div>
              )
            )}
            <label>
              GCP Region
              <select
                value={vm2gkeGcpRegion}
                onChange={(e) => setVm2gkeGcpRegion(e.target.value)}
              >
                <option value="">-- Select region --</option>
                {GCP_REGIONS.map((region) => (
                  <option key={region.id} value={region.id}>
                    {region.display}
                  </option>
                ))}
              </select>
            </label>
          </>
        )}
      </fieldset>
      <fieldset>
        <legend>VM Instance Selection</legend>
        {vm2gkeInstancesLoading && <small>Loading instances...</small>}
        {vm2gkeInstancesError && <div className="error">{vm2gkeInstancesError}</div>}
        {!vm2gkeInstancesLoading && !vm2gkeInstances.length && !vm2gkeInstancesError && (
          <small>
            {vm2gkeProvider === 'gcp' 
              ? 'No instances found in the selected region. Try selecting a different region or verify that instances exist in this region.'
              : 'No instances found. Configure provider settings above and ensure credentials are valid.'}
          </small>
        )}
        <div className="aws-subnet-selector">
          <div className="label-row">
            <label>Select instance to migrate</label>
          </div>
          <div className="checkbox-grid">
            {vm2gkeInstances.map((instance) => {
              const isRunning = vm2gkeProvider === 'aws' 
                ? instance.state === 'running'
                : instance.status === 'RUNNING';
              const isSelected = vm2gkeSelectedInstance === instance.name;
              
              return (
                <label 
                  key={instance.id} 
                  className="checkbox-item"
                >
                  <input
                    type="radio"
                    name="vm2gke-instance"
                    checked={isSelected}
                    onChange={() => {
                      setVm2gkeSelectedInstance(instance.name);
                      // Clear Docker data when instance changes
                      setVm2gkeDockerContainers([]);
                      setVm2gkeSelectedContainers([]);
                      setVm2gkeDockerImages([]);
                      setVm2gkeDockerEnvVars({});
                      setVm2gkeDockerError('');
                      setVm2gkeDockerDiscoveryInitiated(false);
                    }}
                  />
                  <span>
                    {instance.name} ({instance.instance_type || instance.machine_type}) - {isRunning ? '🟢 Running' : '🔴 Stopped'}
                  </span>
                </label>
              );
            })}
          </div>
        </div>
      </fieldset>
      {vm2gkeSelectedInstance && !vm2gkeDockerDiscoveryInitiated && (
        <div style={{ marginTop: '1rem', marginBottom: '1rem' }}>
          <button
            type="button"
            onClick={fetchDockerContainers}
            disabled={
              (vm2gkeProvider === 'aws' && (!authReady || !vm2gkeAwsRegion.trim())) ||
              (vm2gkeProvider === 'gcp' && (!vm2gkeGcpServiceKey.trim() || !vm2gkeGcpProject.trim() || !vm2gkeGcpRegion.trim()))
            }
            style={{
              padding: '0.75rem 1.5rem',
              backgroundColor: '#2563eb',
              color: 'white',
              border: 'none',
              borderRadius: '6px',
              fontSize: '1rem',
              fontWeight: 600,
              cursor: 'pointer',
              transition: 'background-color 0.2s',
            }}
            onMouseOver={(e) => {
              if (!e.currentTarget.disabled) {
                e.currentTarget.style.backgroundColor = '#1d4ed8';
              }
            }}
            onMouseOut={(e) => {
              if (!e.currentTarget.disabled) {
                e.currentTarget.style.backgroundColor = '#2563eb';
              }
            }}
          >
            Proceed to Discover Docker Containers
          </button>
        </div>
      )}
      {vm2gkeSelectedInstance && vm2gkeDockerDiscoveryInitiated && (
        <fieldset>
          <legend>Docker Containers on {vm2gkeSelectedInstance}</legend>
          {vm2gkeDockerLoading && <small>Discovering Docker containers...</small>}
          {vm2gkeDockerError && <div className="error">{vm2gkeDockerError}</div>}
          {!vm2gkeDockerLoading && !vm2gkeDockerContainers.length && !vm2gkeDockerError && (
            <small>No Docker containers found. Docker may not be installed or no containers are running.</small>
          )}
          {vm2gkeDockerContainers.length > 0 && (
            <>
              <div className="aws-subnet-selector">
                <div className="label-row">
                  <label>Select containers to migrate ({vm2gkeSelectedContainers.length} selected)</label>
                  <div className="pill-actions">
                    <button
                      type="button"
                      onClick={() => setVm2gkeSelectedContainers(vm2gkeDockerContainers.map((c) => c.name))}
                      disabled={!vm2gkeDockerContainers.length || vm2gkeDockerLoading}
                    >
                      Select all
                    </button>
                    <button
                      type="button"
                      onClick={() => setVm2gkeSelectedContainers([])}
                      disabled={!vm2gkeSelectedContainers.length || vm2gkeDockerLoading}
                    >
                      Clear
                    </button>
                  </div>
                </div>
                <div className="checkbox-grid">
                  {vm2gkeDockerContainers.map((container, idx) => {
                    const isSelected = vm2gkeSelectedContainers.includes(container.name);
                    return (
                      <label key={idx} className="checkbox-item">
                        <input
                          type="checkbox"
                          checked={isSelected}
                          disabled={vm2gkeDockerLoading}
                          onChange={() => {
                            if (isSelected) {
                              setVm2gkeSelectedContainers(vm2gkeSelectedContainers.filter((name) => name !== container.name));
                            } else {
                              setVm2gkeSelectedContainers([...vm2gkeSelectedContainers, container.name]);
                            }
                          }}
                        />
                        <div style={{ flex: 1 }}>
                          <div style={{ fontWeight: 600, marginBottom: '0.25rem' }}>
                            {container.name || 'unnamed'}
                          </div>
                          <div style={{ fontSize: '0.875rem', color: '#64748b' }}>
                            <div>Image: {container.image || 'unknown'}</div>
                            <div>Status: {container.status || 'unknown'}</div>
                            {vm2gkeDockerEnvVars[container.name] && (
                              <div style={{ marginTop: '0.5rem' }}>
                                <strong>Environment Variables:</strong>
                                <div style={{ marginLeft: '1rem', marginTop: '0.25rem' }}>
                                  {Object.entries(vm2gkeDockerEnvVars[container.name]).slice(0, 5).map(([key, value]) => (
                                    <div key={key} style={{ fontSize: '0.75rem' }}>
                                      {key}={String(value).length > 50 ? String(value).substring(0, 47) + '...' : value}
                                    </div>
                                  ))}
                                  {Object.keys(vm2gkeDockerEnvVars[container.name]).length > 5 && (
                                    <div style={{ fontSize: '0.75rem', fontStyle: 'italic' }}>
                                      ... and {Object.keys(vm2gkeDockerEnvVars[container.name]).length - 5} more
                                    </div>
                                  )}
                                </div>
                              </div>
                            )}
                          </div>
                        </div>
                      </label>
                    );
                  })}
                </div>
              </div>
            </>
          )}
          {vm2gkeDockerImages.length > 0 && (
            <>
              <h4>Docker Images ({vm2gkeDockerImages.length})</h4>
              <div style={{ marginBottom: '1rem' }}>
                {vm2gkeDockerImages.slice(0, 10).map((image, idx) => (
                  <div key={idx} style={{ 
                    padding: '0.5rem', 
                    marginBottom: '0.25rem', 
                    background: '#f8fafc', 
                    borderRadius: '4px',
                    fontSize: '0.875rem'
                  }}>
                    {image.image || 'unknown'}
                  </div>
                ))}
                {vm2gkeDockerImages.length > 10 && (
                  <small>... and {vm2gkeDockerImages.length - 10} more images</small>
                )}
              </div>
            </>
          )}
        </fieldset>
      )}
      <fieldset>
        <legend>Manifest Configuration</legend>
        <label>
          Kubernetes Namespace (optional)
          <input
            value={vm2gkeNamespace}
            onChange={(e) => setVm2gkeNamespace(e.target.value)}
            placeholder="Auto-generated if not specified"
          />
        </label>
      </fieldset>
      <fieldset>
        <legend>Manifest Generation</legend>
        <div className="info-callout">
          Gemini model, fallbacks, and credential settings are controlled via the server <code>.env</code>. Update the backend
          configuration to change these defaults.
        </div>
        <button
          onClick={runVm2GkeManifestTask}
          disabled={
            (vm2gkeProvider === 'aws' && (!authReady || !vm2gkeAwsRegion.trim())) ||
            (vm2gkeProvider === 'gcp' && (!vm2gkeGcpServiceKey.trim() || !vm2gkeGcpProject.trim())) ||
            !vm2gkeSelectedInstance ||
            !vm2gkeSelectedContainers.length
          }
        >
          Generate Kubernetes Manifests
        </button>
        {vm2gkeError && <div className="error">{vm2gkeError}</div>}
        <h3>Logs</h3>
        <pre>{vm2gkeLogs || 'Logs will appear here once a run starts.'}</pre>
        <h3>Artifacts</h3>
        <div className="artifacts">
          {vm2gkeArtifacts.map((artifact) => (
            <a className="download-link" key={artifact.url} href={artifact.url} download={artifact.filename}>
              Download {artifact.filename}
            </a>
          ))}
        </div>
      </fieldset>
      </>
      )}

      {view === 'box_project_aws' && (
      <>
      <fieldset>
        <legend>AWS Configuration</legend>
        <label>
          AWS Region
          <select 
            value={boxAwsSelectedRegion} 
            onChange={(e) => {
              setBoxAwsSelectedRegion(e.target.value);
              setBoxAwsEc2Data({ amis: [], instance_types: [] });
              setBoxAwsRdsData({ engines: [], instance_classes: [] });
              setBoxAwsAvailabilityZones([]);
            }}
            disabled={boxAwsRegionsLoading}
          >
            {boxAwsRegionsLoading && <option>Loading regions...</option>}
            {!boxAwsRegionsLoading && boxAwsRegions.length === 0 && <option>No regions available</option>}
            {boxAwsRegions.map((region) => (
              <option key={region} value={region}>{region}</option>
            ))}
          </select>
        </label>
        {boxAwsRegionsError && <div className="error">{boxAwsRegionsError}</div>}
      </fieldset>
      <fieldset>
        <legend>Services</legend>
        <div className="checkbox-grid">
          {[
            { id: 'vpc', label: 'Amazon VPC' },
            { id: 'ec2', label: 'Amazon EC2' },
            { id: 's3', label: 'Amazon S3' },
            { id: 'rds', label: 'Amazon RDS' },
            { id: 'efs', label: 'Amazon EFS' },
          ].map((service) => (
            <label key={service.id} className="checkbox-item">
              <input
                type="checkbox"
                checked={boxAwsSelectedServices.includes(service.id)}
                onChange={(e) => {
                  if (e.target.checked) {
                    let newServices = [...boxAwsSelectedServices, service.id];
                    // Auto-select VPC when EC2 or RDS is selected
                    if ((service.id === 'ec2' || service.id === 'rds') && !newServices.includes('vpc')) {
                      newServices = [...newServices, 'vpc'];
                    }
                    setBoxAwsSelectedServices(newServices);
                  } else {
                    let newServices = boxAwsSelectedServices.filter(s => s !== service.id);
                    setBoxAwsSelectedServices(newServices);
                    const newConfigs = { ...boxAwsServiceConfigs };
                    delete newConfigs[service.id];
                    setBoxAwsServiceConfigs(newConfigs);
                  }
                }}
              />
              <span>{service.label}</span>
            </label>
          ))}
        </div>
      </fieldset>
      {boxAwsSelectedServices.length > 0 && (
        <fieldset>
          <legend>Service Configuration</legend>
          {/* VPC Configuration */}
          {boxAwsSelectedServices.includes('vpc') && (
            <div className="service-input-card">
              <div style={{ borderBottom: '1px solid #ddd', paddingBottom: '10px', marginBottom: '20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div style={{ flex: 1 }}>
                  <h2 style={{ margin: 0, fontSize: '20px', fontWeight: 'bold' }}>🌐 VPC Configuration</h2>
                  <p style={{ margin: '5px 0 0 0', color: '#666', fontSize: '14px' }}>
                    Configure your Virtual Private Cloud with subnets, Internet Gateway, and NAT Gateway.
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => setBoxAwsServiceExpanded({ ...boxAwsServiceExpanded, vpc: !boxAwsServiceExpanded.vpc })}
                  style={{
                    background: 'none',
                    border: 'none',
                    cursor: 'pointer',
                    padding: '5px 10px',
                    fontSize: '18px',
                    color: '#666',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    minWidth: '30px',
                    height: '30px'
                  }}
                  title={boxAwsServiceExpanded.vpc ? 'Collapse' : 'Expand'}
                >
                  <span style={{ 
                    transform: boxAwsServiceExpanded.vpc ? 'rotate(180deg)' : 'rotate(0deg)',
                    transition: 'transform 0.2s',
                    display: 'inline-block'
                  }}>
                    ▼
                  </span>
                </button>
              </div>
              {boxAwsServiceExpanded.vpc && (
                <div>
                  <label>
                    Number of VPCs
                    <input
                      type="number"
                      min="1"
                      max="10"
                      value={boxAwsVpcCount}
                      onChange={(e) => {
                        const count = parseInt(e.target.value) || 1;
                        setBoxAwsVpcCount(count);
                        // Initialize VPCs array if needed
                        const currentVpcs = boxAwsServiceConfigs.vpc?.vpcs || [];
                        const newVpcs = [];
                        for (let i = 0; i < count; i++) {
                          newVpcs.push(currentVpcs[i] || { name: '', cidr: '10.0.0.0/16', subnets: [] });
                        }
                        setBoxAwsServiceConfigs({
                          ...boxAwsServiceConfigs,
                          vpc: { ...boxAwsServiceConfigs.vpc, vpcs: newVpcs }
                        });
                      }}
                    />
                    <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                      Number of VPCs to create (default: 1)
                    </small>
                  </label>
                  {Array.from({ length: boxAwsVpcCount }).map((_, vpcIdx) => {
                    const vpc = boxAwsServiceConfigs.vpc?.vpcs?.[vpcIdx] || { name: '', cidr: '10.0.0.0/16', subnets: [] };
                    const isExpanded = boxAwsVpcExpanded[vpcIdx] !== false; // Default to expanded
                    return (
                      <div key={vpcIdx} style={{ marginTop: '20px', padding: '15px', border: darkMode ? '2px solid #60a5fa' : '2px solid #0073bb', borderRadius: '4px', backgroundColor: darkMode ? '#1e293b' : '#f0f8ff' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: isExpanded ? '15px' : '0' }}>
                          <h4 style={{ margin: '0', fontSize: '16px', fontWeight: 'bold', color: darkMode ? '#60a5fa' : '#0073bb' }}>
                            🌐 VPC {vpcIdx + 1} {vpc.name ? `- ${vpc.name}` : ''}
                        </h4>
                          <button
                            type="button"
                            onClick={() => setBoxAwsVpcExpanded({ ...boxAwsVpcExpanded, [vpcIdx]: !isExpanded })}
                            style={{
                              background: 'none',
                              border: 'none',
                              cursor: 'pointer',
                              padding: '5px 10px',
                              fontSize: '18px',
                              color: darkMode ? '#60a5fa' : '#0073bb',
                              display: 'flex',
                              alignItems: 'center',
                              justifyContent: 'center',
                              minWidth: '30px',
                              height: '30px'
                            }}
                            title={isExpanded ? 'Collapse' : 'Expand'}
                          >
                            <span style={{ 
                              transform: isExpanded ? 'rotate(180deg)' : 'rotate(0deg)',
                              transition: 'transform 0.2s',
                              display: 'inline-block'
                            }}>
                              ▼
                            </span>
                          </button>
                        </div>
                        {isExpanded && (
                        <>
                        <label>
                          VPC Name
                          <input
                            value={vpc.name || ''}
                            onChange={(e) => {
                              const vpcs = [...(boxAwsServiceConfigs.vpc?.vpcs || [])];
                              while (vpcs.length <= vpcIdx) {
                                vpcs.push({ name: '', cidr: '10.0.0.0/16', subnets: [] });
                              }
                              vpcs[vpcIdx] = { ...vpcs[vpcIdx], name: e.target.value };
                              setBoxAwsServiceConfigs({
                                ...boxAwsServiceConfigs,
                                vpc: { ...boxAwsServiceConfigs.vpc, vpcs }
                              });
                            }}
                            placeholder={`vpc-${vpcIdx + 1}`}
                          />
                          <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                            Name for this VPC (e.g., production-vpc, dev-vpc)
                          </small>
                        </label>
                        <label style={{ marginTop: '10px', display: 'block' }}>
                          VPC CIDR Block
                          <input
                            value={vpc.cidr || '10.0.0.0/16'}
                            onChange={(e) => {
                              const vpcs = [...(boxAwsServiceConfigs.vpc?.vpcs || [])];
                              while (vpcs.length <= vpcIdx) {
                                vpcs.push({ name: '', cidr: '10.0.0.0/16', subnets: [] });
                              }
                              vpcs[vpcIdx] = { ...vpcs[vpcIdx], cidr: e.target.value };
                              setBoxAwsServiceConfigs({
                                ...boxAwsServiceConfigs,
                                vpc: { ...boxAwsServiceConfigs.vpc, vpcs }
                              });
                            }}
                            placeholder="10.0.0.0/16"
                          />
                          <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                            Example: 10.0.0.0/16 (provides 65,536 IP addresses)
                          </small>
                        </label>
                        <label style={{ marginTop: '10px', display: 'block' }}>
                          Total Number of Subnets
                          <input
                            type="number"
                            min="1"
                            max="10"
                            value={vpc.subnets?.length || boxAwsVpcTotalSubnets}
                            onChange={(e) => {
                              const num = parseInt(e.target.value) || 1;
                              const vpcs = [...(boxAwsServiceConfigs.vpc?.vpcs || [])];
                              while (vpcs.length <= vpcIdx) {
                                vpcs.push({ name: '', cidr: '10.0.0.0/16', subnets: [] });
                              }
                              const currentSubnets = vpcs[vpcIdx].subnets || [];
                              const newSubnets = [];
                              for (let i = 0; i < num; i++) {
                                newSubnets.push(currentSubnets[i] || { cidr: '', type: 'public', name: '' });
                              }
                              vpcs[vpcIdx] = { ...vpcs[vpcIdx], subnets: newSubnets };
                              setBoxAwsServiceConfigs({
                                ...boxAwsServiceConfigs,
                                vpc: { ...boxAwsServiceConfigs.vpc, vpcs }
                              });
                            }}
                          />
                        </label>
                        <div style={{ marginTop: '20px' }}>
                          <h5 style={{ fontSize: '14px', fontWeight: 'bold', marginBottom: '10px' }}>Subnet Configuration</h5>
                          {Array.from({ length: vpc.subnets?.length || boxAwsVpcTotalSubnets }).map((_, idx) => {
                            const subnet = vpc.subnets?.[idx] || { cidr: '', type: 'public', name: '' };
                            return (
                              <div key={idx} style={{ marginBottom: '15px', padding: '15px', border: '1px solid #ddd', borderRadius: '4px' }}>
                                <h6 style={{ margin: '0 0 10px 0', fontSize: '13px', fontWeight: 'bold' }}>Subnet {idx + 1}</h6>
                                <label style={{ display: 'block', marginBottom: '10px' }}>
                                  Subnet Name
                                  <input
                                    type="text"
                                    value={subnet.name || ''}
                                    onChange={(e) => {
                                      const vpcs = [...(boxAwsServiceConfigs.vpc?.vpcs || [])];
                                      while (vpcs.length <= vpcIdx) {
                                        vpcs.push({ name: '', cidr: '10.0.0.0/16', subnets: [] });
                                      }
                                      const subnets = [...(vpcs[vpcIdx].subnets || [])];
                                      while (subnets.length <= idx) {
                                        subnets.push({ cidr: '', type: 'public', name: '' });
                                      }
                                      subnets[idx] = { ...subnets[idx], name: e.target.value };
                                      vpcs[vpcIdx] = { ...vpcs[vpcIdx], subnets };
                                      setBoxAwsServiceConfigs({
                                        ...boxAwsServiceConfigs,
                                        vpc: { ...boxAwsServiceConfigs.vpc, vpcs }
                                      });
                                    }}
                                    placeholder={`subnet-${idx + 1}`}
                                    style={{ width: '100%', padding: '8px', fontSize: '14px', marginTop: '5px' }}
                                  />
                                </label>
                                <label style={{ display: 'block', marginBottom: '10px' }}>
                                  CIDR Block
                                  <input
                                    type="text"
                                    value={subnet.cidr || ''}
                                    onChange={(e) => {
                                      const vpcs = [...(boxAwsServiceConfigs.vpc?.vpcs || [])];
                                      while (vpcs.length <= vpcIdx) {
                                        vpcs.push({ name: '', cidr: '10.0.0.0/16', subnets: [] });
                                      }
                                      const subnets = [...(vpcs[vpcIdx].subnets || [])];
                                      while (subnets.length <= idx) {
                                        subnets.push({ cidr: '', type: 'public', name: '' });
                                      }
                                      subnets[idx] = { ...subnets[idx], cidr: e.target.value };
                                      vpcs[vpcIdx] = { ...vpcs[vpcIdx], subnets };
                                      setBoxAwsServiceConfigs({
                                        ...boxAwsServiceConfigs,
                                        vpc: { ...boxAwsServiceConfigs.vpc, vpcs }
                                      });
                                    }}
                                    placeholder={`10.${vpcIdx}.${idx + 1}.0/24`}
                                    style={{ width: '100%', padding: '8px', fontSize: '14px', marginTop: '5px' }}
                                  />
                                </label>
                                <label style={{ display: 'block' }}>
                                  Subnet Type
                                  <select
                                    value={subnet.type || 'public'}
                                    onChange={(e) => {
                                      const vpcs = [...(boxAwsServiceConfigs.vpc?.vpcs || [])];
                                      while (vpcs.length <= vpcIdx) {
                                        vpcs.push({ name: '', cidr: '10.0.0.0/16', subnets: [] });
                                      }
                                      const subnets = [...(vpcs[vpcIdx].subnets || [])];
                                      while (subnets.length <= idx) {
                                        subnets.push({ cidr: '', type: 'public', name: '' });
                                      }
                                      subnets[idx] = { ...subnets[idx], type: e.target.value };
                                      
                                      // Auto-enable IGW if there are any public subnets
                                      const hasPublicSubnets = subnets.some(s => s.type === 'public');
                                      // Auto-enable NAT Gateway if there are any private subnets
                                      const hasPrivateSubnets = subnets.some(s => s.type === 'private');
                                      
                                      vpcs[vpcIdx] = { 
                                        ...vpcs[vpcIdx], 
                                        subnets,
                                        enable_internet_gateway: hasPublicSubnets,
                                        enable_nat_gateway: hasPrivateSubnets
                                      };
                                      setBoxAwsServiceConfigs({
                                        ...boxAwsServiceConfigs,
                                        vpc: { ...boxAwsServiceConfigs.vpc, vpcs }
                                      });
                                    }}
                                    style={{ width: '100%', padding: '8px', fontSize: '14px', marginTop: '5px' }}
                                  >
                                    <option value="public">Public Subnet (Internet Gateway)</option>
                                    <option value="private">Private Subnet (NAT Gateway)</option>
                                  </select>
                                  <small style={{ display: 'block', marginTop: '8px', padding: '10px', backgroundColor: darkMode ? '#334155' : '#f0f9ff', border: darkMode ? '1px solid #475569' : '1px solid #bfdbfe', borderRadius: '4px', fontSize: '13px', lineHeight: '1.5', color: darkMode ? '#cbd5e1' : '#334155' }}>
                                    <strong>📌 Quick Guide:</strong><br/>
                                    • <strong>Public Subnet</strong>: Resources can communicate directly with the internet via Internet Gateway (IGW). Use for web servers, load balancers.<br/>
                                    • <strong>Private Subnet</strong>: Resources have no direct internet access. Can access internet for updates via NAT Gateway (outbound only). Use for databases, app servers.
                                  </small>
                                </label>
                              </div>
                            );
                          })}
                        </div>
                        <div style={{ marginTop: '20px', padding: '15px', backgroundColor: darkMode ? '#334155' : '#f0f8ff', borderRadius: '4px', border: darkMode ? '1px solid #475569' : '1px solid #b3d9ff' }}>
                          <h5 style={{ margin: '0 0 10px 0', fontSize: '14px', fontWeight: 'bold', color: darkMode ? '#60a5fa' : '#0066cc' }}>Gateway Configuration</h5>
                          <div style={{ fontSize: '14px', color: darkMode ? '#cbd5e1' : '#333' }}>
                            {(() => {
                              const subnets = vpc.subnets || [];
                              const hasPublicSubnets = subnets.some(s => s.type === 'public');
                              const hasPrivateSubnets = subnets.some(s => s.type === 'private');
                              return (
                                <>
                                  {hasPublicSubnets && (
                                    <div style={{ marginBottom: '8px', display: 'flex', alignItems: 'center' }}>
                                      <span style={{ color: '#28a745', marginRight: '8px', fontSize: '16px' }}>✓</span>
                                      <span><strong>Internet Gateway (IGW)</strong> will be enabled for public subnets</span>
                                    </div>
                                  )}
                                  {hasPrivateSubnets && (
                                    <div style={{ display: 'flex', alignItems: 'center' }}>
                                      <span style={{ color: '#28a745', marginRight: '8px', fontSize: '16px' }}>✓</span>
                                      <span><strong>NAT Gateway</strong> will be enabled for private subnets</span>
                                    </div>
                                  )}
                                  {!hasPublicSubnets && !hasPrivateSubnets && (
                                    <div style={{ color: '#666', fontStyle: 'italic' }}>
                                      Configure subnets above to automatically enable gateways
                                    </div>
                                  )}
                                </>
                              );
                            })()}
                          </div>
                        </div>
                        </>
                        )}
                      </div>
                    );
                  })}

                  {/* VPC Summary */}
                  <div style={{ marginTop: '25px', padding: '20px', backgroundColor: darkMode ? '#0f172a' : '#ecfdf5', borderRadius: '12px', border: darkMode ? '2px solid #10b981' : '2px solid #059669' }}>
                    <h3 style={{ margin: '0 0 15px 0', fontSize: '16px', fontWeight: 'bold', color: darkMode ? '#34d399' : '#047857', display: 'flex', alignItems: 'center', gap: '10px' }}>
                      📊 VPC Configuration Summary
                      <span style={{ fontSize: '11px', fontWeight: 'normal', backgroundColor: (boxAwsServiceConfigs.vpc?.name && boxAwsServiceConfigs.vpc?.cidr) ? '#22c55e' : '#f59e0b', color: 'white', padding: '3px 8px', borderRadius: '10px' }}>
                        {(boxAwsServiceConfigs.vpc?.name && boxAwsServiceConfigs.vpc?.cidr) ? 'Ready' : 'Needs Config'}
                      </span>
                    </h3>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: '12px' }}>
                      {/* VPC Name */}
                      <div style={{ padding: '12px', backgroundColor: darkMode ? '#1e293b' : 'white', borderRadius: '8px', border: '1px solid #ddd', textAlign: 'center' }}>
                        <div style={{ fontSize: '20px', marginBottom: '5px' }}>🏷️</div>
                        <div style={{ fontSize: '12px', fontWeight: 'bold', color: darkMode ? '#e2e8f0' : '#333', wordBreak: 'break-all' }}>
                          {boxAwsServiceConfigs.vpc?.name || 'Not set'}
                        </div>
                        <div style={{ fontSize: '11px', color: '#666' }}>VPC Name</div>
                      </div>
                      {/* CIDR Block */}
                      <div style={{ padding: '12px', backgroundColor: darkMode ? '#1e293b' : 'white', borderRadius: '8px', border: '1px solid #ddd', textAlign: 'center' }}>
                        <div style={{ fontSize: '20px', marginBottom: '5px' }}>🔢</div>
                        <div style={{ fontSize: '12px', fontWeight: 'bold', color: darkMode ? '#e2e8f0' : '#333' }}>
                          {boxAwsServiceConfigs.vpc?.cidr || '10.0.0.0/16'}
                        </div>
                        <div style={{ fontSize: '11px', color: '#666' }}>CIDR Block</div>
                      </div>
                      {/* Total Subnets */}
                      <div style={{ padding: '12px', backgroundColor: darkMode ? '#1e293b' : 'white', borderRadius: '8px', border: '1px solid #ddd', textAlign: 'center' }}>
                        <div style={{ fontSize: '20px', marginBottom: '5px' }}>🔀</div>
                        <div style={{ fontSize: '12px', fontWeight: 'bold', color: darkMode ? '#e2e8f0' : '#333' }}>
                          {boxAwsServiceConfigs.vpc?.subnets?.length || 0}
                        </div>
                        <div style={{ fontSize: '11px', color: '#666' }}>Total Subnets</div>
                      </div>
                      {/* Public Subnets */}
                      <div style={{ padding: '12px', backgroundColor: darkMode ? '#1e293b' : 'white', borderRadius: '8px', border: '1px solid #ddd', textAlign: 'center' }}>
                        <div style={{ fontSize: '20px', marginBottom: '5px' }}>🌍</div>
                        <div style={{ fontSize: '12px', fontWeight: 'bold', color: '#16a34a' }}>
                          {boxAwsServiceConfigs.vpc?.subnets?.filter(s => s.type === 'public').length || 0}
                        </div>
                        <div style={{ fontSize: '11px', color: '#666' }}>Public Subnets</div>
                      </div>
                      {/* Private Subnets */}
                      <div style={{ padding: '12px', backgroundColor: darkMode ? '#1e293b' : 'white', borderRadius: '8px', border: '1px solid #ddd', textAlign: 'center' }}>
                        <div style={{ fontSize: '20px', marginBottom: '5px' }}>🔒</div>
                        <div style={{ fontSize: '12px', fontWeight: 'bold', color: '#dc2626' }}>
                          {boxAwsServiceConfigs.vpc?.subnets?.filter(s => s.type === 'private').length || 0}
                        </div>
                        <div style={{ fontSize: '11px', color: '#666' }}>Private Subnets</div>
                      </div>
                      {/* Gateway Status */}
                      <div style={{ padding: '12px', backgroundColor: darkMode ? '#1e293b' : 'white', borderRadius: '8px', border: '1px solid #ddd', textAlign: 'center' }}>
                        <div style={{ fontSize: '20px', marginBottom: '5px' }}>🚀</div>
                        <div style={{ fontSize: '12px', fontWeight: 'bold', color: darkMode ? '#e2e8f0' : '#333' }}>
                          {(() => {
                            const hasPublic = boxAwsServiceConfigs.vpc?.subnets?.some(s => s.type === 'public');
                            const hasPrivate = boxAwsServiceConfigs.vpc?.subnets?.some(s => s.type === 'private');
                            if (hasPublic && hasPrivate) return 'IGW + NAT';
                            if (hasPublic) return 'IGW Only';
                            if (hasPrivate) return 'NAT Only';
                            return 'None';
                          })()}
                        </div>
                        <div style={{ fontSize: '11px', color: '#666' }}>Gateways</div>
                      </div>
                    </div>

                    {/* Subnet Details */}
                    {boxAwsServiceConfigs.vpc?.subnets?.length > 0 && (
                      <div style={{ marginTop: '15px' }}>
                        <h4 style={{ fontSize: '13px', fontWeight: 'bold', color: darkMode ? '#e2e8f0' : '#333', marginBottom: '10px' }}>🔗 Subnet Details</h4>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
                          {boxAwsServiceConfigs.vpc.subnets.map((subnet, idx) => (
                            <div 
                              key={idx}
                              style={{ 
                                padding: '8px 12px', 
                                backgroundColor: subnet.type === 'public' ? (darkMode ? '#064e3b' : '#d1fae5') : (darkMode ? '#7f1d1d' : '#fee2e2'), 
                                borderRadius: '6px', 
                                fontSize: '12px',
                                border: `1px solid ${subnet.type === 'public' ? '#10b981' : '#ef4444'}`
                              }}
                            >
                              <strong>{subnet.name || `Subnet ${idx + 1}`}</strong>
                              <span style={{ margin: '0 5px', opacity: 0.7 }}>|</span>
                              <span>{subnet.cidr || 'No CIDR'}</span>
                              <span style={{ margin: '0 5px', opacity: 0.7 }}>|</span>
                              <span style={{ color: subnet.type === 'public' ? '#10b981' : '#ef4444', fontWeight: 'bold' }}>
                                {subnet.type === 'public' ? '🌍 Public' : '🔒 Private'}
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          )}
          {boxAwsSelectedServices.includes('ec2') && (
            <div className="service-input-card" style={{ maxWidth: '100%' }}>
              <div style={{ borderBottom: '1px solid #ddd', paddingBottom: '10px', marginBottom: '20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div style={{ flex: 1 }}>
                  <h2 style={{ margin: 0, fontSize: '20px', fontWeight: 'bold' }}>🖥️ Launch EC2 Instances</h2>
                  <p style={{ margin: '5px 0 0 0', color: '#666', fontSize: '14px' }}>
                    Create {boxAwsEc2Instances.length} virtual machine{boxAwsEc2Instances.length > 1 ? 's' : ''} on the AWS Cloud.
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => setBoxAwsServiceExpanded({ ...boxAwsServiceExpanded, ec2: !boxAwsServiceExpanded.ec2 })}
                  style={{
                    background: 'none',
                    border: 'none',
                    cursor: 'pointer',
                    padding: '5px 10px',
                    fontSize: '18px',
                    color: '#666',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    minWidth: '30px',
                    height: '30px'
                  }}
                  title={boxAwsServiceExpanded.ec2 ? 'Collapse' : 'Expand'}
                >
                  <span style={{ 
                    transform: boxAwsServiceExpanded.ec2 ? 'rotate(180deg)' : 'rotate(0deg)',
                    transition: 'transform 0.2s',
                    display: 'inline-block'
                  }}>
                    ▼
                  </span>
                </button>
              </div>
              
              {boxAwsServiceExpanded.ec2 && (
              <div>
              {/* Instance Count Control */}
              <div style={{ 
                marginBottom: '20px', 
                padding: '15px', 
                backgroundColor: darkMode ? '#1e3a5f' : '#e0f2fe', 
                borderRadius: '8px',
                border: darkMode ? '1px solid #3b82f6' : '1px solid #0284c7'
              }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '15px' }}>
                  <div>
                    <strong style={{ color: darkMode ? '#60a5fa' : '#0369a1' }}>Number of Instances</strong>
                    <p style={{ margin: '5px 0 0 0', fontSize: '13px', color: darkMode ? '#93c5fd' : '#0c4a6e' }}>
                      Each instance can have different configurations (name, type, storage, etc.)
                    </p>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <button
                      type="button"
                      onClick={() => {
                        if (boxAwsEc2Instances.length > 1) {
                          setBoxAwsEc2Instances(boxAwsEc2Instances.slice(0, -1));
                        }
                      }}
                      disabled={boxAwsEc2Instances.length <= 1}
                      style={{
                        width: '36px',
                        height: '36px',
                        border: '1px solid #ddd',
                        borderRadius: '4px',
                        background: boxAwsEc2Instances.length <= 1 ? '#f5f5f5' : 'white',
                        cursor: boxAwsEc2Instances.length <= 1 ? 'not-allowed' : 'pointer',
                        fontSize: '20px',
                        fontWeight: 'bold',
                        color: boxAwsEc2Instances.length <= 1 ? '#ccc' : '#333'
                      }}
                    >
                      −
                    </button>
                    <span style={{ 
                      minWidth: '50px', 
                      textAlign: 'center', 
                      fontSize: '20px', 
                      fontWeight: 'bold',
                      color: darkMode ? '#60a5fa' : '#0369a1'
                    }}>
                      {boxAwsEc2Instances.length}
                    </span>
                    <button
                      type="button"
                      onClick={() => {
                        const newId = Math.max(...boxAwsEc2Instances.map(i => i.id)) + 1;
                        setBoxAwsEc2Instances([
                          ...boxAwsEc2Instances,
                          { id: newId, name: `server-${newId}`, expanded: true, ebsVolumes: [], keyPairSelection: 'select' }
                        ]);
                        setBoxAwsEc2InstancesExpanded({ ...boxAwsEc2InstancesExpanded, [newId]: true });
                      }}
                      disabled={boxAwsEc2Instances.length >= 10}
                      style={{
                        width: '36px',
                        height: '36px',
                        border: '1px solid #0073bb',
                        borderRadius: '4px',
                        background: boxAwsEc2Instances.length >= 10 ? '#f5f5f5' : '#0073bb',
                        cursor: boxAwsEc2Instances.length >= 10 ? 'not-allowed' : 'pointer',
                        fontSize: '20px',
                        fontWeight: 'bold',
                        color: boxAwsEc2Instances.length >= 10 ? '#ccc' : 'white'
                      }}
                    >
                      +
                    </button>
                  </div>
                </div>
              </div>

              {/* Individual Instance Cards */}
              {boxAwsEc2Instances.map((instance, instanceIdx) => (
                <div 
                  key={instance.id} 
                  style={{ 
                    marginBottom: '20px', 
                    border: darkMode ? '2px solid #475569' : '2px solid #0073bb', 
                    borderRadius: '8px',
                    overflow: 'hidden'
                  }}
                >
                  {/* Instance Header */}
                  <div 
                    style={{ 
                      padding: '12px 15px', 
                      backgroundColor: darkMode ? '#334155' : '#0073bb',
                      color: 'white',
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      cursor: 'pointer'
                    }}
                    onClick={() => setBoxAwsEc2InstancesExpanded({ 
                      ...boxAwsEc2InstancesExpanded, 
                      [instance.id]: !boxAwsEc2InstancesExpanded[instance.id] 
                    })}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                      <span style={{ 
                        backgroundColor: 'rgba(255,255,255,0.2)', 
                        padding: '4px 10px', 
                        borderRadius: '4px',
                        fontSize: '14px',
                        fontWeight: 'bold'
                      }}>
                        #{instanceIdx + 1}
                      </span>
                      <span style={{ fontWeight: 'bold', fontSize: '16px' }}>
                        {boxAwsServiceConfigs.ec2?.instances?.[instance.id]?.name || instance.name || `Instance ${instanceIdx + 1}`}
                      </span>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                      {boxAwsEc2Instances.length > 1 && (
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            setBoxAwsEc2Instances(boxAwsEc2Instances.filter(i => i.id !== instance.id));
                            const newConfigs = { ...boxAwsServiceConfigs };
                            if (newConfigs.ec2?.instances) {
                              delete newConfigs.ec2.instances[instance.id];
                            }
                            setBoxAwsServiceConfigs(newConfigs);
                          }}
                          style={{
                            background: 'rgba(255,255,255,0.2)',
                            border: 'none',
                            color: 'white',
                            padding: '4px 8px',
                            borderRadius: '4px',
                            cursor: 'pointer',
                            fontSize: '12px'
                          }}
                          title="Remove this instance"
                        >
                          🗑️ Remove
                        </button>
                      )}
                      <span style={{ 
                        transform: boxAwsEc2InstancesExpanded[instance.id] ? 'rotate(180deg)' : 'rotate(0deg)',
                        transition: 'transform 0.2s',
                        display: 'inline-block'
                      }}>
                        ▼
                      </span>
                    </div>
                  </div>

                  {/* Instance Configuration (Expandable) */}
                  {boxAwsEc2InstancesExpanded[instance.id] && (
                    <div style={{ padding: '20px', backgroundColor: darkMode ? '#1e293b' : '#f8fafc' }}>
                      {/* Name and tags */}
                      <div style={{ marginBottom: '20px', border: '1px solid #ddd', borderRadius: '4px', padding: '15px', backgroundColor: darkMode ? '#0f172a' : 'white' }}>
                        <h4 style={{ marginTop: 0, fontSize: '15px', fontWeight: 'bold', color: darkMode ? '#e2e8f0' : '#333' }}>
                          🏷️ Name and Tags
                        </h4>
                        <label style={{ display: 'block', marginBottom: '15px' }}>
                          Instance Name
                    <input
                            type="text"
                            value={boxAwsServiceConfigs.ec2?.instances?.[instance.id]?.name || instance.name || ''}
                      onChange={(e) => {
                              const instances = { ...(boxAwsServiceConfigs.ec2?.instances || {}) };
                              instances[instance.id] = { ...instances[instance.id], name: e.target.value };
                        setBoxAwsServiceConfigs({
                          ...boxAwsServiceConfigs,
                                ec2: { ...boxAwsServiceConfigs.ec2, instances }
                        });
                              // Also update local state
                              setBoxAwsEc2Instances(boxAwsEc2Instances.map(i => 
                                i.id === instance.id ? { ...i, name: e.target.value } : i
                              ));
                      }}
                            placeholder={`e.g., web-server-${instanceIdx + 1}, api-server`}
                            style={{ width: '100%', marginTop: '5px', padding: '8px', fontSize: '14px' }}
                    />
                          <small style={{ color: '#666', display: 'block', marginTop: '5px' }}>
                            A unique name to identify this instance (will be used as AWS Name tag)
                    </small>
                  </label>
                        <label style={{ display: 'block' }}>
                          Additional Tags (comma-separated key=value)
                    <input
                            type="text"
                            value={boxAwsServiceConfigs.ec2?.instances?.[instance.id]?.tags || ''}
                      onChange={(e) => {
                              const instances = { ...(boxAwsServiceConfigs.ec2?.instances || {}) };
                              instances[instance.id] = { ...instances[instance.id], tags: e.target.value };
                        setBoxAwsServiceConfigs({
                          ...boxAwsServiceConfigs,
                                ec2: { ...boxAwsServiceConfigs.ec2, instances }
                        });
                      }}
                            placeholder="Environment=Production,Owner=DevTeam,Project=MyApp"
                            style={{ width: '100%', marginTop: '5px', padding: '8px', fontSize: '14px' }}
                    />
                          <small style={{ color: '#666', display: 'block', marginTop: '5px' }}>
                            Example: Env=prod,Owner=team,Purpose=webserver
                          </small>
                  </label>
                      </div>

                      {/* Security Groups */}
                      {boxAwsSelectedServices.includes('vpc') && (
                        <div style={{ marginBottom: '20px', border: '1px solid #ddd', borderRadius: '4px', padding: '15px', backgroundColor: darkMode ? '#0f172a' : 'white' }}>
                          <h4 style={{ marginTop: 0, fontSize: '15px', fontWeight: 'bold', color: darkMode ? '#e2e8f0' : '#333' }}>
                            🛡️ Security Groups
                          </h4>
                          <p style={{ fontSize: '13px', color: '#666', marginBottom: '15px' }}>
                            Security groups act as virtual firewalls to control inbound and outbound traffic to your instance.
                          </p>
                          <label style={{ display: 'block', marginBottom: '15px' }}>
                            Security Group Name
                            <input
                              type="text"
                              value={boxAwsServiceConfigs.ec2?.instances?.[instance.id]?.security_group_name || ''}
                              onChange={(e) => {
                                const instances = { ...(boxAwsServiceConfigs.ec2?.instances || {}) };
                                instances[instance.id] = { ...instances[instance.id], security_group_name: e.target.value };
                                setBoxAwsServiceConfigs({
                                  ...boxAwsServiceConfigs,
                                  ec2: { ...boxAwsServiceConfigs.ec2, instances }
                                });
                              }}
                              placeholder={`${(boxAwsServiceConfigs.ec2?.instances?.[instance.id]?.name || instance.name || 'instance')}-sg`}
                              style={{ width: '100%', marginTop: '5px', padding: '8px', fontSize: '14px' }}
                            />
                          </label>

                          {/* Security Group Rules */}
                          <div style={{ marginTop: '15px' }}>
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '10px' }}>
                              <strong style={{ fontSize: '14px', color: darkMode ? '#e2e8f0' : '#333' }}>Inbound Rules</strong>
                              <button
                                type="button"
                                onClick={() => {
                                  const newId = Math.max(0, ...boxAwsEc2SecurityGroupRules.map(r => r.id)) + 1;
                                  setBoxAwsEc2SecurityGroupRules([
                                    ...boxAwsEc2SecurityGroupRules,
                                    { id: newId, port: 80, protocol: 'tcp', cidr: '0.0.0.0/0', description: 'Custom' }
                                  ]);
                                }}
                                style={{ 
                                  padding: '4px 10px', 
                                  fontSize: '12px', 
                                  backgroundColor: '#0073bb', 
                                  color: 'white', 
                                  border: 'none', 
                                  borderRadius: '4px', 
                                  cursor: 'pointer' 
                                }}
                              >
                                + Add Rule
                              </button>
                            </div>

                            {/* Rules Table */}
                            <div style={{ overflowX: 'auto' }}>
                              <table style={{ width: '100%', fontSize: '13px', borderCollapse: 'collapse' }}>
                                <thead>
                                  <tr style={{ backgroundColor: darkMode ? '#1e293b' : '#f3f4f6' }}>
                                    <th style={{ padding: '8px', textAlign: 'left', border: '1px solid #ddd' }}>Port</th>
                                    <th style={{ padding: '8px', textAlign: 'left', border: '1px solid #ddd' }}>Protocol</th>
                                    <th style={{ padding: '8px', textAlign: 'left', border: '1px solid #ddd' }}>Source IP/CIDR</th>
                                    <th style={{ padding: '8px', textAlign: 'left', border: '1px solid #ddd' }}>Description</th>
                                    <th style={{ padding: '8px', textAlign: 'center', border: '1px solid #ddd', width: '60px' }}>Action</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {boxAwsEc2SecurityGroupRules.map((rule) => (
                                    <tr key={rule.id} style={{ backgroundColor: darkMode ? '#0f172a' : 'white' }}>
                                      <td style={{ padding: '6px', border: '1px solid #ddd' }}>
                                        <input
                                          type="number"
                                          value={rule.port}
                                          onChange={(e) => {
                                            setBoxAwsEc2SecurityGroupRules(boxAwsEc2SecurityGroupRules.map(r => 
                                              r.id === rule.id ? { ...r, port: parseInt(e.target.value) || 0 } : r
                                            ));
                                          }}
                                          style={{ width: '70px', padding: '4px', fontSize: '13px' }}
                                          min="0"
                                          max="65535"
                                        />
                                      </td>
                                      <td style={{ padding: '6px', border: '1px solid #ddd' }}>
                                        <select
                                          value={rule.protocol}
                                          onChange={(e) => {
                                            setBoxAwsEc2SecurityGroupRules(boxAwsEc2SecurityGroupRules.map(r => 
                                              r.id === rule.id ? { ...r, protocol: e.target.value } : r
                                            ));
                                          }}
                                          style={{ width: '80px', padding: '4px', fontSize: '13px' }}
                                        >
                                          <option value="tcp">TCP</option>
                                          <option value="udp">UDP</option>
                                          <option value="icmp">ICMP</option>
                                          <option value="all">All</option>
                                        </select>
                                      </td>
                                      <td style={{ padding: '6px', border: '1px solid #ddd' }}>
                            <input
                              type="text"
                                          value={rule.cidr}
                              onChange={(e) => {
                                            setBoxAwsEc2SecurityGroupRules(boxAwsEc2SecurityGroupRules.map(r => 
                                              r.id === rule.id ? { ...r, cidr: e.target.value } : r
                                            ));
                                          }}
                                          placeholder="0.0.0.0/0"
                                          style={{ width: '100%', padding: '4px', fontSize: '13px' }}
                                        />
                                      </td>
                                      <td style={{ padding: '6px', border: '1px solid #ddd' }}>
                                        <input
                                          type="text"
                                          value={rule.description}
                                          onChange={(e) => {
                                            setBoxAwsEc2SecurityGroupRules(boxAwsEc2SecurityGroupRules.map(r => 
                                              r.id === rule.id ? { ...r, description: e.target.value } : r
                                            ));
                                          }}
                                          placeholder="Description"
                                          style={{ width: '100%', padding: '4px', fontSize: '13px' }}
                                        />
                                      </td>
                                      <td style={{ padding: '6px', border: '1px solid #ddd', textAlign: 'center' }}>
                                        <button
                                          type="button"
                                          onClick={() => {
                                            if (boxAwsEc2SecurityGroupRules.length > 1) {
                                              setBoxAwsEc2SecurityGroupRules(boxAwsEc2SecurityGroupRules.filter(r => r.id !== rule.id));
                                            } else {
                                              alert('At least one security group rule is required.');
                                            }
                                          }}
                                          style={{ 
                                            background: 'none', 
                                            border: 'none', 
                                            cursor: 'pointer', 
                                            fontSize: '16px',
                                            color: '#dc2626'
                                          }}
                                          title="Remove rule"
                                        >
                                          🗑️
                                        </button>
                                      </td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>

                            <div style={{ marginTop: '10px', padding: '10px', backgroundColor: darkMode ? '#1e3a8a' : '#dbeafe', borderRadius: '6px', fontSize: '12px' }}>
                              <strong style={{ color: darkMode ? '#93c5fd' : '#1e40af' }}>💡 Common Ports:</strong>
                              <div style={{ marginTop: '5px', color: darkMode ? '#bfdbfe' : '#1e3a8a', lineHeight: '1.6' }}>
                                • SSH: 22 (Linux/Mac remote access)<br/>
                                • RDP: 3389 (Windows remote access)<br/>
                                • HTTP: 80 (Web traffic)<br/>
                                • HTTPS: 443 (Secure web traffic)<br/>
                                • MySQL: 3306, PostgreSQL: 5432, MongoDB: 27017<br/>
                                • Use 0.0.0.0/0 for public access, or specify your IP (e.g., 1.2.3.4/32) for restricted access
                              </div>
                            </div>
                          </div>
                        </div>
                      )}

                      {/* SSH Key Pair */}
                      <div style={{ marginBottom: '20px', border: '1px solid #ddd', borderRadius: '4px', padding: '15px', backgroundColor: darkMode ? '#0f172a' : 'white' }}>
                        <h4 style={{ marginTop: 0, fontSize: '15px', fontWeight: 'bold', color: darkMode ? '#e2e8f0' : '#333' }}>
                          🔑 SSH Key Pair
                        </h4>
                        <p style={{ fontSize: '13px', color: '#666', marginBottom: '15px' }}>
                          Select or create an SSH key pair to connect to this instance.
                        </p>
                        
                        {/* Key Pair Dropdown */}
                        <label style={{ display: 'block', marginBottom: '15px' }}>
                          Key Pair Selection
                          <select
                            value={boxAwsEc2Instances.find(i => i.id === instance.id)?.keyPairSelection || 'select'}
                            onChange={(e) => {
                              setBoxAwsEc2Instances(boxAwsEc2Instances.map(i => 
                                i.id === instance.id ? { ...i, keyPairSelection: e.target.value } : i
                              ));
                              // If selecting existing key, update config
                              if (e.target.value !== 'create-new' && e.target.value !== 'select') {
                                const selectedKey = boxAwsEc2KeyPairList.find(k => k.name === e.target.value);
                                if (selectedKey) {
                                  const instances = { ...(boxAwsServiceConfigs.ec2?.instances || {}) };
                                  instances[instance.id] = { 
                                    ...instances[instance.id], 
                                    key_name: selectedKey.name,
                                    public_key: selectedKey.public_key 
                                  };
                                setBoxAwsServiceConfigs({
                                  ...boxAwsServiceConfigs,
                                    ec2: { ...boxAwsServiceConfigs.ec2, instances }
                                });
                                }
                              }
                              }}
                              style={{ width: '100%', padding: '8px', fontSize: '14px', marginTop: '5px' }}
                          >
                            <option value="select">-- Select Key Pair --</option>
                            <option value="create-new">➕ Create New Key</option>
                            {boxAwsEc2KeyPairList.map((keyPair) => (
                              <option key={keyPair.name} value={keyPair.name}>
                                🔑 {keyPair.name} {keyPair.locked ? '(Locked)' : ''}
                              </option>
                            ))}
                          </select>
                          </label>

                        {/* Create New Key UI */}
                        {boxAwsEc2Instances.find(i => i.id === instance.id)?.keyPairSelection === 'create-new' && (
                          <div style={{ padding: '15px', backgroundColor: darkMode ? '#1e293b' : '#f3f4f6', borderRadius: '6px', marginBottom: '15px' }}>
                            <label style={{ display: 'block', marginBottom: '10px' }}>
                              New Key Pair Name
                              <input
                                type="text"
                                value={boxAwsEc2Instances.find(i => i.id === instance.id)?.newKeyName || ''}
                              onChange={(e) => {
                                  setBoxAwsEc2Instances(boxAwsEc2Instances.map(i => 
                                    i.id === instance.id ? { ...i, newKeyName: e.target.value } : i
                                  ));
                                }}
                                placeholder={`my-key-pair-${instance.id}`}
                                style={{ width: '100%', marginTop: '5px', padding: '8px', fontSize: '14px' }}
                              />
                            </label>
                            <button
                              type="button"
                              onClick={async () => {
                                const newKeyName = boxAwsEc2Instances.find(i => i.id === instance.id)?.newKeyName?.trim();
                                if (!newKeyName) {
                                  alert('Please enter a key pair name');
                                  return;
                                }
                                // Check if key already exists
                                if (boxAwsEc2KeyPairList.find(k => k.name === newKeyName)) {
                                  alert('A key pair with this name already exists. Please choose a different name.');
                                  return;
                                }
                                
                                const confirmMsg = `Generate key pair "${newKeyName}"?`;
                                if (!confirm(confirmMsg)) {
                                  return;
                                }
                                
                                try {
                                  const res = await postJson('/api/box/aws/generate-key-pair/', {
                                    key_name: newKeyName
                                  });
                                  
                                  if (!res.private_key || !res.public_key) {
                                    throw new Error('Invalid response from server: missing key data');
                                  }
                                  
                                  // Add to key pair list
                                  setBoxAwsEc2KeyPairList([
                                    ...boxAwsEc2KeyPairList,
                                    { name: res.key_name, public_key: res.public_key, locked: true }
                                  ]);
                                  
                                  // Update instance to use this key
                                  const instances = { ...(boxAwsServiceConfigs.ec2?.instances || {}) };
                                  instances[instance.id] = { 
                                    ...instances[instance.id], 
                                    key_name: res.key_name,
                                    public_key: res.public_key 
                                  };
                                setBoxAwsServiceConfigs({
                                  ...boxAwsServiceConfigs,
                                    ec2: { ...boxAwsServiceConfigs.ec2, instances }
                                  });
                                  
                                  // Change selection to the new key
                                  setBoxAwsEc2Instances(boxAwsEc2Instances.map(i => 
                                    i.id === instance.id ? { ...i, keyPairSelection: res.key_name, newKeyName: '' } : i
                                  ));
                                  
                                  // Show the generated keys in a modal
                                  setBoxAwsGeneratedKeyPair({
                                    key_name: res.key_name,
                                    private_key: res.private_key,
                                    public_key: res.public_key
                                  });
                                } catch (err) {
                                  console.error('Failed to generate key pair:', err);
                                  const errorMessage = err.message || err.toString() || 'Unknown error occurred';
                                  alert(`Failed to generate key pair: ${errorMessage}`);
                                }
                              }}
                              style={{
                                padding: '8px 16px',
                                background: '#10b981',
                                color: 'white',
                                border: 'none',
                                borderRadius: '4px',
                                cursor: 'pointer',
                                fontSize: '14px',
                                fontWeight: 'bold'
                              }}
                            >
                              🔐 Generate Key Pair
                            </button>
                              </div>
                            )}

                        {/* Show locked key info */}
                        {boxAwsEc2Instances.find(i => i.id === instance.id)?.keyPairSelection && 
                         boxAwsEc2Instances.find(i => i.id === instance.id)?.keyPairSelection !== 'select' &&
                         boxAwsEc2Instances.find(i => i.id === instance.id)?.keyPairSelection !== 'create-new' && (
                          <div style={{ padding: '12px', backgroundColor: darkMode ? '#064e3b' : '#d1fae5', borderRadius: '6px', border: '1px solid #10b981' }}>
                            <div style={{ color: darkMode ? '#34d399' : '#047857', fontWeight: 'bold', marginBottom: '5px' }}>
                              ✓ Key "{boxAwsEc2Instances.find(i => i.id === instance.id)?.keyPairSelection}" assigned
                              </div>
                            <div style={{ color: darkMode ? '#a7f3d0' : '#065f46', fontSize: '13px', marginBottom: '10px' }}>
                              This key pair is locked for this instance. You can reuse it for other instances from the dropdown.
                </div>
                <button
                  type="button"
                              onClick={() => {
                                if (confirm('Regenerate key pair? This will create a completely new key.')) {
                                  setBoxAwsEc2Instances(boxAwsEc2Instances.map(i => 
                                    i.id === instance.id ? { ...i, keyPairSelection: 'create-new', newKeyName: '' } : i
                                  ));
                                }
                              }}
                  style={{
                                padding: '6px 12px',
                                background: '#f59e0b',
                                color: 'white',
                    border: 'none',
                                borderRadius: '4px',
                    cursor: 'pointer',
                                fontSize: '13px',
                                fontWeight: 'bold'
                  }}
                            >
                              🔄 Regenerate
                </button>
              </div>
                        )}

                        <div style={{ marginTop: '10px', padding: '10px', backgroundColor: darkMode ? '#1e3a8a' : '#dbeafe', borderRadius: '6px', fontSize: '12px' }}>
                          <strong style={{ color: darkMode ? '#93c5fd' : '#1e40af' }}>💡 Tip:</strong>
                          <div style={{ marginTop: '5px', color: darkMode ? '#bfdbfe' : '#1e3a8a', lineHeight: '1.5' }}>
                            • Create one key and reuse it for multiple instances<br/>
                            • Download and save your private key securely - you'll need it to connect<br/>
                            • Once locked, you can regenerate if needed
                          </div>
                        </div>
                      </div>

                      {/* User Data */}
                      <div style={{ marginBottom: '20px', border: '1px solid #ddd', borderRadius: '4px', padding: '15px', backgroundColor: darkMode ? '#0f172a' : 'white' }}>
                        <h4 style={{ marginTop: 0, fontSize: '15px', fontWeight: 'bold', color: darkMode ? '#e2e8f0' : '#333' }}>
                          📜 User Data (Optional)
                        </h4>
                        <p style={{ fontSize: '13px', color: '#666', marginBottom: '15px' }}>
                          User data scripts run when the instance first starts. Use this to automate instance setup.
                        </p>
                        <label style={{ display: 'block' }}>
                          User Data Script (bash)
                          <textarea
                            value={boxAwsServiceConfigs.ec2?.instances?.[instance.id]?.user_data || ''}
                    onChange={(e) => {
                              const instances = { ...(boxAwsServiceConfigs.ec2?.instances || {}) };
                              instances[instance.id] = { ...instances[instance.id], user_data: e.target.value };
                      setBoxAwsServiceConfigs({
                        ...boxAwsServiceConfigs,
                                ec2: { ...boxAwsServiceConfigs.ec2, instances }
                      });
                    }}
                            placeholder={`#!/bin/bash\n# Install web server\nyum update -y\nyum install -y httpd\nsystemctl start httpd\nsystemctl enable httpd`}
                            style={{ width: '100%', marginTop: '5px', padding: '8px', fontSize: '13px', fontFamily: 'monospace', minHeight: '100px' }}
                  />
                          <small style={{ color: '#666', display: 'block', marginTop: '5px' }}>
                            Must start with #!/bin/bash. This script runs as root on first boot.
                          </small>
                </label>
              </div>

              {/* Application and OS Images */}
                      <div style={{ marginBottom: '20px', border: '1px solid #ddd', borderRadius: '4px', padding: '15px', backgroundColor: darkMode ? '#0f172a' : 'white' }}>
                        <h4 style={{ marginTop: 0, fontSize: '15px', fontWeight: 'bold', color: darkMode ? '#e2e8f0' : '#333' }}>
                          Application and OS Images (AMI)
                        </h4>
                        <p style={{ fontSize: '13px', color: '#666', marginBottom: '15px' }}>
                          Select the operating system and software for this instance.
                </p>
                
                {/* Quick Start Tabs */}
                <div style={{ display: 'flex', gap: '10px', marginBottom: '15px', borderBottom: '1px solid #ddd', paddingBottom: '10px' }}>
                  <button
                    type="button"
                    onClick={() => setBoxAwsEc2AmiTab('quick-start')}
                    style={{
                      padding: '8px 16px',
                      border: 'none',
                      background: boxAwsEc2AmiTab === 'quick-start' ? '#0073bb' : 'transparent',
                      color: boxAwsEc2AmiTab === 'quick-start' ? 'white' : '#0073bb',
                      cursor: 'pointer',
                      borderRadius: '4px',
                      fontWeight: boxAwsEc2AmiTab === 'quick-start' ? 'bold' : 'normal'
                    }}
                  >
                    Quick Start
                  </button>
                  <button
                    type="button"
                    onClick={() => setBoxAwsEc2AmiTab('my-amis')}
                    style={{
                      padding: '8px 16px',
                      border: 'none',
                      background: boxAwsEc2AmiTab === 'my-amis' ? '#0073bb' : 'transparent',
                      color: boxAwsEc2AmiTab === 'my-amis' ? 'white' : '#0073bb',
                      cursor: 'pointer',
                      borderRadius: '4px',
                      fontWeight: boxAwsEc2AmiTab === 'my-amis' ? 'bold' : 'normal'
                    }}
                  >
                    My AMIs
                  </button>
                </div>

                {boxAwsEc2AmiTab === 'quick-start' && (
                  <>
                    {/* OS Type Selection */}
                    <div style={{ marginBottom: '15px' }}>
                      <label style={{ display: 'block', marginBottom: '8px', fontWeight: 'bold' }}>Operating System</label>
                      <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
                        {['amazon-linux', 'ubuntu', 'windows', 'rhel', 'suse', 'debian'].map((os) => (
                          <button
                            key={os}
                            type="button"
                            onClick={() => {
                              setBoxAwsEc2OsType(os);
                              const defaultVersions = {
                                'amazon-linux': '2023',
                                'ubuntu': '22.04',
                                'windows': '2022',
                                'rhel': '9',
                                'suse': '15',
                                'debian': '12'
                              };
                              // Don't auto-set version - let user select to avoid unwanted loading
                              setBoxAwsEc2OsVersion('');
                              setBoxAwsEc2Data({ ...boxAwsEc2Data, amis: [] });
                            }}
                            style={{
                              padding: '10px 20px',
                              border: `2px solid ${boxAwsEc2OsType === os ? '#0073bb' : '#ddd'}`,
                              background: boxAwsEc2OsType === os ? '#e6f4fa' : 'white',
                              color: boxAwsEc2OsType === os ? '#0073bb' : '#333',
                              cursor: 'pointer',
                              borderRadius: '4px',
                              fontWeight: boxAwsEc2OsType === os ? 'bold' : 'normal'
                            }}
                          >
                            {os === 'amazon-linux' ? 'Amazon Linux' : 
                             os === 'ubuntu' ? 'Ubuntu' :
                             os === 'windows' ? 'Windows' :
                             os === 'rhel' ? 'Red Hat' :
                             os === 'suse' ? 'SUSE Linux' : 'Debian'}
                          </button>
                        ))}
                      </div>
                    </div>

                    {/* Version Selection */}
                    <div style={{ marginBottom: '15px' }}>
                      <label style={{ display: 'block', marginBottom: '8px', fontWeight: 'bold' }}>Version</label>
                      <select
                        value={boxAwsEc2OsVersion || ''}
                        onChange={(e) => {
                          setBoxAwsEc2OsVersion(e.target.value);
                          if (e.target.value) {
                            setBoxAwsEc2Data({ ...boxAwsEc2Data, amis: [] });
                          }
                        }}
                        style={{ width: '100%', padding: '8px', fontSize: '14px' }}
                      >
                        <option value="">Select version...</option>
                        {boxAwsEc2OsType === 'amazon-linux' && (
                          <>
                            <option value="2023">Amazon Linux 2023</option>
                            <option value="2022">Amazon Linux 2022</option>
                            <option value="latest">Latest</option>
                          </>
                        )}
                        {boxAwsEc2OsType === 'ubuntu' && (
                          <>
                            <option value="24.04">Ubuntu 24.04 LTS</option>
                            <option value="22.04">Ubuntu 22.04 LTS</option>
                            <option value="20.04">Ubuntu 20.04 LTS</option>
                            <option value="latest">Latest</option>
                          </>
                        )}
                        {boxAwsEc2OsType === 'windows' && (
                          <>
                            <option value="2022">Windows Server 2022</option>
                            <option value="2019">Windows Server 2019</option>
                            <option value="2016">Windows Server 2016</option>
                            <option value="latest">Latest</option>
                          </>
                        )}
                        {boxAwsEc2OsType === 'rhel' && (
                          <>
                            <option value="9">RHEL 9</option>
                            <option value="8">RHEL 8</option>
                            <option value="7">RHEL 7</option>
                            <option value="latest">Latest</option>
                          </>
                        )}
                        {boxAwsEc2OsType === 'suse' && (
                          <>
                            <option value="15">SUSE Linux Enterprise 15</option>
                            <option value="12">SUSE Linux Enterprise 12</option>
                            <option value="latest">Latest</option>
                          </>
                        )}
                        {boxAwsEc2OsType === 'debian' && (
                          <>
                            <option value="12">Debian 12</option>
                            <option value="11">Debian 11</option>
                            <option value="latest">Latest</option>
                          </>
                        )}
                      </select>
                    </div>

                    {/* AMI Cards */}
                    {boxAwsEc2DataLoading && <div style={{ padding: '20px', textAlign: 'center' }}>Loading latest AMIs...</div>}
                    {!boxAwsEc2DataLoading && (boxAwsEc2Data.amis || []).length === 0 && boxAwsSelectedRegion && (
                      <div style={{ padding: '20px', textAlign: 'center', color: '#666' }}>
                        No AMIs found. Please check your AWS credentials and region.
                      </div>
                    )}
                    {!boxAwsEc2DataLoading && (boxAwsEc2Data.amis || []).length > 0 && (
                      <div style={{ display: 'grid', gap: '15px' }}>
                        {(boxAwsEc2Data.amis || []).map((ami) => (
                          <div
                            key={ami.id}
                            onClick={() => {
                              setBoxAwsEc2SelectedAmi(ami);
                              setBoxAwsServiceConfigs({
                                ...boxAwsServiceConfigs,
                                ec2: { ...boxAwsServiceConfigs.ec2, ami: ami.id }
                              });
                            }}
                            style={{
                              border: `2px solid ${boxAwsEc2SelectedAmi?.id === ami.id ? '#0073bb' : '#ddd'}`,
                              borderRadius: '4px',
                              padding: '15px',
                              cursor: 'pointer',
                              background: boxAwsEc2SelectedAmi?.id === ami.id ? '#e6f4fa' : 'white',
                              transition: 'all 0.2s'
                            }}
                          >
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'start' }}>
                              <div>
                                <h4 style={{ margin: 0, fontSize: '16px', fontWeight: 'bold' }}>{ami.name}</h4>
                                <div style={{ fontSize: '12px', color: '#666', marginTop: '5px' }}>
                                  <div>AMI ID: {ami.id}</div>
                                  {ami.description && (
                                    <div style={{ marginTop: '5px', fontSize: '13px', color: '#333' }}>{ami.description.substring(0, 100)}{ami.description.length > 100 ? '...' : ''}</div>
                                  )}
                                </div>
                              </div>
                              {boxAwsEc2SelectedAmi?.id === ami.id && (
                                <span style={{ color: '#0073bb', fontWeight: 'bold' }}>✓ Selected</span>
                              )}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </>
                )}

                {boxAwsEc2AmiTab === 'my-amis' && (
                  <div style={{ padding: '20px', textAlign: 'center', color: '#666' }}>
                    My AMIs feature - Coming soon. Use Quick Start for now.
                  </div>
                )}
              </div>

              {/* Instance type */}
              <div style={{ marginBottom: '30px', border: '1px solid #ddd', borderRadius: '4px', padding: '15px' }}>
                <h3 style={{ marginTop: 0, fontSize: '16px', fontWeight: 'bold' }}>
                  Instance type <span style={{ fontSize: '12px', color: '#666', fontWeight: 'normal' }}>Info | Get advice</span>
                </h3>
                <label>
                  Instance type
                  <select
                    value={boxAwsServiceConfigs.ec2?.instance_type || ''}
                    onChange={(e) => {
                      setBoxAwsServiceConfigs({
                        ...boxAwsServiceConfigs,
                        ec2: { ...boxAwsServiceConfigs.ec2, instance_type: e.target.value }
                      });
                    }}
                    disabled={boxAwsEc2DataLoading}
                    style={{ width: '100%', padding: '8px', fontSize: '14px', marginTop: '8px' }}
                  >
                    <option value="">Select instance type...</option>
                    {(boxAwsEc2Data.instance_types || []).map((type) => {
                      const details = (boxAwsEc2Data.instance_type_details || []).find(d => d.instance_type === type);
                      return (
                        <option key={type} value={type}>
                          {type}
                          {details && ` - ${details.vcpu} vCPU, ${details.memory_gib} GiB Memory`}
                          {details?.free_tier_eligible && ' (Free tier eligible)'}
                        </option>
                      );
                    })}
                  </select>
                </label>
                {boxAwsServiceConfigs.ec2?.instance_type && (() => {
                  const details = (boxAwsEc2Data.instance_type_details || []).find(d => d.instance_type === boxAwsServiceConfigs.ec2?.instance_type);
                  return details ? (
                    <div style={{ marginTop: '10px', padding: '10px', background: '#f5f5f5', borderRadius: '4px', fontSize: '14px' }}>
                      <div><strong>Family:</strong> {details.family}</div>
                      <div><strong>vCPU:</strong> {details.vcpu}</div>
                      <div><strong>Memory:</strong> {details.memory_gib} GiB</div>
                      <div><strong>Current generation:</strong> {details.current_generation ? 'Yes' : 'No'}</div>
                      {details.free_tier_eligible && <div style={{ color: '#0073bb', fontWeight: 'bold' }}>Free tier eligible</div>}
                    </div>
                  ) : null;
                })()}
              </div>

              {/* Network Settings (VPC and Subnet Selection) */}
              <div style={{ marginBottom: '30px', border: '1px solid #ddd', borderRadius: '4px', padding: '15px' }}>
                <h3 style={{ marginTop: 0, fontSize: '16px', fontWeight: 'bold' }}>
                  Network Settings <span style={{ fontSize: '12px', color: '#666', fontWeight: 'normal' }}>Info</span>
                </h3>
                <p style={{ fontSize: '14px', color: '#666', marginBottom: '15px' }}>
                  Select the VPC and subnet where you want to launch your EC2 instance.
                </p>
                {boxAwsSelectedServices.includes('vpc') && boxAwsServiceConfigs.vpc?.vpcs?.length > 0 ? (
                  <>
                    <label>
                      VPC
                      <select
                        value={boxAwsServiceConfigs.ec2?.vpc_id || ''}
                        onChange={(e) => {
                          setBoxAwsServiceConfigs({
                            ...boxAwsServiceConfigs,
                            ec2: { ...boxAwsServiceConfigs.ec2, vpc_id: e.target.value, subnet_id: '' }
                          });
                        }}
                        style={{ width: '100%', padding: '8px', fontSize: '14px', marginTop: '8px' }}
                      >
                        <option value="">Select VPC...</option>
                        {boxAwsServiceConfigs.vpc.vpcs.map((vpc, vpcIdx) => (
                          <option key={vpcIdx} value={`vpc-${vpcIdx}`}>
                            {vpc.name || `VPC ${vpcIdx + 1}`} - {vpc.cidr || 'Not configured'}
                          </option>
                        ))}
                      </select>
                    </label>
                    {boxAwsServiceConfigs.ec2?.vpc_id && (() => {
                      const selectedVpcIdx = parseInt(boxAwsServiceConfigs.ec2.vpc_id.replace('vpc-', ''));
                      const selectedVpc = boxAwsServiceConfigs.vpc?.vpcs?.[selectedVpcIdx];
                      return selectedVpc ? (
                        <label style={{ marginTop: '15px', display: 'block' }}>
                          Subnet
                          <select
                            value={boxAwsServiceConfigs.ec2?.subnet_id || ''}
                            onChange={(e) => {
                              setBoxAwsServiceConfigs({
                                ...boxAwsServiceConfigs,
                                ec2: { ...boxAwsServiceConfigs.ec2, subnet_id: e.target.value }
                              });
                            }}
                            style={{ width: '100%', padding: '8px', fontSize: '14px', marginTop: '8px' }}
                          >
                            <option value="">Select subnet...</option>
                            {selectedVpc.subnets?.map((subnet, idx) => (
                              <option key={idx} value={`module.vpc_${selectedVpcIdx}.${subnet.type}_subnet_ids[${idx}]`}>
                                {subnet.name || `Subnet ${idx + 1}`} - {subnet.cidr || 'Not configured'} ({subnet.type === 'public' ? 'Public' : 'Private'})
                              </option>
                            ))}
                          </select>
                          <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                            Select a subnet from the selected VPC. Public subnets allow direct internet access.
                          </small>
                        </label>
                      ) : null;
                    })()}
                  </>
                ) : (
                  <div style={{ padding: '15px', background: '#fff3cd', borderRadius: '4px', border: '1px solid #ffc107' }}>
                    <strong>No VPCs configured.</strong> Please configure VPCs in the VPC Configuration section above.
                  </div>
                )}
              </div>

              {/* Configure storage */}
              <div style={{ marginBottom: '30px', border: '1px solid #ddd', borderRadius: '4px', padding: '15px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '15px' }}>
                  <h3 style={{ margin: 0, fontSize: '16px', fontWeight: 'bold' }}>
                    Configure storage <span style={{ fontSize: '12px', color: '#666', fontWeight: 'normal' }}>Info</span>
                  </h3>
                  <button type="button" style={{ padding: '5px 10px', border: '1px solid #ddd', background: 'white', cursor: 'pointer' }}>Advanced</button>
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'auto 1fr auto auto auto', gap: '10px', alignItems: 'center', marginBottom: '10px' }}>
                  <div style={{ fontWeight: 'bold' }}>1x</div>
                  <input
                    type="number"
                    min="8"
                    value={boxAwsEc2StorageSize}
                    onChange={(e) => {
                      setBoxAwsEc2StorageSize(parseInt(e.target.value) || 8);
                      setBoxAwsServiceConfigs({
                        ...boxAwsServiceConfigs,
                        ec2: { ...boxAwsServiceConfigs.ec2, root_volume_size: parseInt(e.target.value) || 8 }
                      });
                    }}
                    style={{ padding: '8px', fontSize: '14px' }}
                  />
                  <div>GiB</div>
                  <select
                    value={boxAwsEc2StorageType}
                    onChange={(e) => {
                      setBoxAwsEc2StorageType(e.target.value);
                      setBoxAwsServiceConfigs({
                        ...boxAwsServiceConfigs,
                        ec2: { ...boxAwsServiceConfigs.ec2, root_volume_type: e.target.value }
                      });
                    }}
                    style={{ padding: '8px', fontSize: '14px' }}
                  >
                    <option value="gp3">gp3</option>
                    <option value="gp2">gp2</option>
                    <option value="io1">io1</option>
                    <option value="io2">io2</option>
                  </select>
                  <div style={{ fontSize: '14px' }}>
                    Root volume,
                    {boxAwsEc2StorageType === 'gp3' && (
                      <>
                        <input
                          type="number"
                          min="3000"
                          max="16000"
                          value={boxAwsEc2StorageIops}
                          onChange={(e) => {
                            setBoxAwsEc2StorageIops(parseInt(e.target.value) || 3000);
                            setBoxAwsServiceConfigs({
                              ...boxAwsServiceConfigs,
                              ec2: { ...boxAwsServiceConfigs.ec2, root_volume_iops: parseInt(e.target.value) || 3000 }
                            });
                          }}
                          style={{ width: '60px', padding: '4px', margin: '0 5px' }}
                        />
                        IOPS,
                      </>
                    )}
                    <label style={{ marginLeft: '10px', fontSize: '14px' }}>
                      <input
                        type="checkbox"
                        checked={boxAwsEc2StorageEncrypted}
                        onChange={(e) => {
                          setBoxAwsEc2StorageEncrypted(e.target.checked);
                          setBoxAwsServiceConfigs({
                            ...boxAwsServiceConfigs,
                            ec2: { ...boxAwsServiceConfigs.ec2, root_volume_encrypted: e.target.checked }
                          });
                        }}
                        style={{ marginRight: '5px' }}
                      />
                      {boxAwsEc2StorageEncrypted ? 'Encrypted' : 'Not encrypted'}
                    </label>
                  </div>
                </div>
                <div style={{ fontSize: '12px', color: '#666', marginTop: '10px' }}>
                  Free tier eligible customers can get up to 30 GB of EBS General Purpose (SSD) or Magnetic storage
                </div>
              </div>

                      {/* Per-Instance EBS Volumes */}
                      <div style={{ marginTop: '20px', marginBottom: '20px', border: '1px solid #ddd', borderRadius: '4px', padding: '15px', backgroundColor: darkMode ? '#0f172a' : 'white' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '15px' }}>
                          <div>
                            <h4 style={{ margin: 0, fontSize: '15px', fontWeight: 'bold', color: darkMode ? '#e2e8f0' : '#333' }}>
                              💾 Additional EBS Volumes
                            </h4>
                            <p style={{ fontSize: '13px', color: '#666', margin: '5px 0 0 0' }}>
                              Add extra storage volumes for this instance
                            </p>
                </div>
                <button
                  type="button"
                  onClick={() => {
                              const currentVolumes = instance.ebsVolumes || [];
                              const newVolId = Math.max(0, ...currentVolumes.map(v => v.id || 0)) + 1;
                              setBoxAwsEc2Instances(boxAwsEc2Instances.map(i => 
                                i.id === instance.id ? { 
                                  ...i, 
                                  ebsVolumes: [
                                    ...currentVolumes,
                                    { id: newVolId, name: `volume-${newVolId}`, size: 20, type: 'gp3', iops: 3000, encrypted: true, expanded: true }
                                  ]
                                } : i
                              ));
                  }}
                            style={{
                              padding: '6px 12px',
                              backgroundColor: '#f59e0b',
                              color: 'white',
                              border: 'none',
                              borderRadius: '4px',
                              cursor: 'pointer',
                              fontSize: '13px',
                              fontWeight: 'bold'
                            }}
                >
                            + Add Volume
                </button>
              </div>

                        {(!instance.ebsVolumes || instance.ebsVolumes.length === 0) ? (
                          <div style={{ padding: '15px', textAlign: 'center', color: '#666', backgroundColor: darkMode ? '#1e293b' : '#f9fafb', borderRadius: '4px', border: '1px dashed #ddd' }}>
                            No additional volumes. Click "+ Add Volume" to add storage.
                          </div>
                        ) : (
                          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                            {instance.ebsVolumes.map((volume, volIdx) => (
                              <div key={volume.id} style={{ border: '1px solid #ddd', borderRadius: '4px', overflow: 'hidden', backgroundColor: darkMode ? '#1e293b' : 'white' }}>
                                {/* Volume Header */}
                                <div 
                                  style={{ 
                                    padding: '10px 15px', 
                                    backgroundColor: darkMode ? '#374151' : '#fbbf24',
                                    color: darkMode ? 'white' : '#78350f',
                                    display: 'flex',
                                    justifyContent: 'space-between',
                                    alignItems: 'center',
                                    cursor: 'pointer'
                                  }}
                                  onClick={() => {
                                    setBoxAwsEc2Instances(boxAwsEc2Instances.map(i => 
                                      i.id === instance.id ? {
                                        ...i,
                                        ebsVolumes: i.ebsVolumes.map(v => 
                                          v.id === volume.id ? { ...v, expanded: !v.expanded } : v
                                        )
                                      } : i
                                    ));
                                  }}
                                >
                                  <div style={{ fontWeight: 'bold', fontSize: '14px' }}>
                                    💾 Volume {volIdx + 1}: {volume.name || `volume-${volume.id}`} ({volume.size || 20} GiB, {volume.type || 'gp3'})
                                  </div>
                                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                                    <button
                                      type="button"
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        if (confirm('Remove this volume?')) {
                                          setBoxAwsEc2Instances(boxAwsEc2Instances.map(i => 
                                            i.id === instance.id ? {
                                              ...i,
                                              ebsVolumes: i.ebsVolumes.filter(v => v.id !== volume.id)
                                            } : i
                                          ));
                                        }
                                      }}
                                      style={{
                                        background: 'rgba(255,255,255,0.2)',
                                        border: 'none',
                                        color: darkMode ? 'white' : '#78350f',
                                        padding: '4px 8px',
                                        borderRadius: '4px',
                                        cursor: 'pointer',
                                        fontSize: '12px'
                                      }}
                                    >
                                      🗑️ Remove
                                    </button>
                                    <span style={{ transform: volume.expanded ? 'rotate(180deg)' : 'rotate(0deg)', transition: 'transform 0.2s' }}>▼</span>
                                  </div>
                                </div>

                                {/* Volume Configuration */}
                                {volume.expanded && (
                                  <div style={{ padding: '15px', backgroundColor: darkMode ? '#0f172a' : 'white' }}>
                                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '15px' }}>
                                      <label style={{ display: 'block' }}>
                                        <span style={{ fontSize: '12px', fontWeight: 'bold', color: darkMode ? '#60a5fa' : '#0073bb' }}>Volume Name</span>
                                        <input
                                          type="text"
                                          value={volume.name || ''}
                                          onChange={(e) => {
                                            setBoxAwsEc2Instances(boxAwsEc2Instances.map(i => 
                                              i.id === instance.id ? {
                                                ...i,
                                                ebsVolumes: i.ebsVolumes.map(v => 
                                                  v.id === volume.id ? { ...v, name: e.target.value } : v
                                                )
                                              } : i
                                            ));
                                          }}
                                          placeholder={`volume-${volume.id}`}
                                          style={{ width: '100%', padding: '6px 8px', marginTop: '4px', fontSize: '13px' }}
                                        />
                                      </label>
                                      
                                      <label style={{ display: 'block' }}>
                                        <span style={{ fontSize: '12px', fontWeight: 'bold', color: darkMode ? '#60a5fa' : '#0073bb' }}>Size (GiB)</span>
                                        <input
                                          type="number"
                                          min="1"
                                          value={volume.size || 20}
                                          onChange={(e) => {
                                            setBoxAwsEc2Instances(boxAwsEc2Instances.map(i => 
                                              i.id === instance.id ? {
                                                ...i,
                                                ebsVolumes: i.ebsVolumes.map(v => 
                                                  v.id === volume.id ? { ...v, size: parseInt(e.target.value) || 20 } : v
                                                )
                                              } : i
                                            ));
                                          }}
                                          style={{ width: '100%', padding: '6px 8px', marginTop: '4px', fontSize: '13px' }}
                                        />
                                      </label>
                                      
                                      <label style={{ display: 'block' }}>
                                        <span style={{ fontSize: '12px', fontWeight: 'bold', color: darkMode ? '#60a5fa' : '#0073bb' }}>Volume Type</span>
                                        <select
                                          value={volume.type || 'gp3'}
                                          onChange={(e) => {
                                            setBoxAwsEc2Instances(boxAwsEc2Instances.map(i => 
                                              i.id === instance.id ? {
                                                ...i,
                                                ebsVolumes: i.ebsVolumes.map(v => 
                                                  v.id === volume.id ? { ...v, type: e.target.value } : v
                                                )
                                              } : i
                                            ));
                                          }}
                                          style={{ width: '100%', padding: '6px 8px', marginTop: '4px', fontSize: '13px' }}
                                        >
                                          <option value="gp3">gp3 (General Purpose SSD)</option>
                                          <option value="gp2">gp2 (General Purpose SSD)</option>
                                          <option value="io1">io1 (Provisioned IOPS SSD)</option>
                                          <option value="io2">io2 (Provisioned IOPS SSD)</option>
                                          <option value="st1">st1 (Throughput Optimized HDD)</option>
                                          <option value="sc1">sc1 (Cold HDD)</option>
                                        </select>
                                      </label>
                                      
                                      {(volume.type === 'gp3' || volume.type === 'io1' || volume.type === 'io2') && (
                                        <label style={{ display: 'block' }}>
                                          <span style={{ fontSize: '12px', fontWeight: 'bold', color: darkMode ? '#60a5fa' : '#0073bb' }}>IOPS</span>
                                          <input
                                            type="number"
                                            min="100"
                                            value={volume.iops || 3000}
                                            onChange={(e) => {
                                              setBoxAwsEc2Instances(boxAwsEc2Instances.map(i => 
                                                i.id === instance.id ? {
                                                  ...i,
                                                  ebsVolumes: i.ebsVolumes.map(v => 
                                                    v.id === volume.id ? { ...v, iops: parseInt(e.target.value) || 3000 } : v
                                                  )
                                                } : i
                                              ));
                                            }}
                                            style={{ width: '100%', padding: '6px 8px', marginTop: '4px', fontSize: '13px' }}
                                          />
                                        </label>
                                      )}
                                      
                                      <label style={{ display: 'flex', alignItems: 'center', gap: '8px', marginTop: '20px' }}>
                                        <input
                                          type="checkbox"
                                          checked={volume.encrypted !== false}
                                          onChange={(e) => {
                                            setBoxAwsEc2Instances(boxAwsEc2Instances.map(i => 
                                              i.id === instance.id ? {
                                                ...i,
                                                ebsVolumes: i.ebsVolumes.map(v => 
                                                  v.id === volume.id ? { ...v, encrypted: e.target.checked } : v
                                                )
                                              } : i
                                            ));
                                          }}
                                        />
                                        <span style={{ fontSize: '13px', fontWeight: 'bold', color: darkMode ? '#60a5fa' : '#0073bb' }}>Encrypted</span>
                                      </label>
                                    </div>
                                  </div>
                                )}
                </div>
                            ))}
                          </div>
                        )}
                      </div>

                      {/* Instance Summary */}
                      <div style={{ marginTop: '20px', padding: '15px', backgroundColor: darkMode ? '#334155' : '#e0f2fe', borderRadius: '8px', border: darkMode ? '1px solid #475569' : '1px solid #0284c7' }}>
                        <h4 style={{ margin: '0 0 10px 0', fontSize: '14px', fontWeight: 'bold', color: darkMode ? '#60a5fa' : '#0369a1' }}>
                          Instance #{instanceIdx + 1} Summary
                        </h4>
                        <div style={{ fontSize: '13px', lineHeight: '1.8', color: darkMode ? '#cbd5e1' : '#334155' }}>
                          <div><strong>Name:</strong> {boxAwsServiceConfigs.ec2?.instances?.[instance.id]?.name || instance.name || 'Not set'}</div>
                          <div><strong>AMI:</strong> {boxAwsServiceConfigs.ec2?.instances?.[instance.id]?.ami || boxAwsEc2SelectedAmi?.id || 'Not selected'}</div>
                          <div><strong>Instance Type:</strong> {boxAwsServiceConfigs.ec2?.instances?.[instance.id]?.instance_type || boxAwsServiceConfigs.ec2?.instance_type || 'Not selected'}</div>
                          <div><strong>Root Volume:</strong> {boxAwsServiceConfigs.ec2?.instances?.[instance.id]?.root_volume_size || boxAwsEc2StorageSize || 8} GiB</div>
                          <div><strong>Additional Volumes:</strong> {instance.ebsVolumes?.length || 0} volume{instance.ebsVolumes?.length !== 1 ? 's' : ''}</div>
                          <div><strong>Key Pair:</strong> {boxAwsServiceConfigs.ec2?.instances?.[instance.id]?.key_name || 'Not configured'}</div>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              ))}

              {/* Overall EC2 Summary - Enhanced */}
              <div style={{ marginTop: '20px', padding: '20px', backgroundColor: darkMode ? '#0f172a' : '#f0fdf4', borderRadius: '12px', border: darkMode ? '2px solid #22c55e' : '2px solid #16a34a' }}>
                <h3 style={{ margin: '0 0 20px 0', fontSize: '18px', fontWeight: 'bold', color: darkMode ? '#22c55e' : '#15803d', display: 'flex', alignItems: 'center', gap: '10px' }}>
                  📊 EC2 Configuration Summary
                  <span style={{ fontSize: '12px', fontWeight: 'normal', backgroundColor: darkMode ? '#22c55e' : '#16a34a', color: 'white', padding: '3px 10px', borderRadius: '12px' }}>
                    Ready to Deploy
                  </span>
                </h3>

                {/* Main Stats Row */}
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: '12px', marginBottom: '20px' }}>
                  {/* Instances Card */}
                  <div style={{ padding: '15px', backgroundColor: darkMode ? '#1e293b' : 'white', borderRadius: '10px', border: darkMode ? '1px solid #3b82f6' : '1px solid #0073bb', position: 'relative', overflow: 'hidden' }}>
                    <div style={{ position: 'absolute', top: '-10px', right: '-10px', fontSize: '50px', opacity: 0.1 }}>🖥️</div>
                    <div style={{ fontSize: '32px', fontWeight: 'bold', color: darkMode ? '#60a5fa' : '#0073bb' }}>
                      {boxAwsEc2Instances.length}
                    </div>
                    <div style={{ fontSize: '13px', color: darkMode ? '#94a3b8' : '#666', fontWeight: '500' }}>EC2 Instances</div>
                  </div>

                  {/* Root Storage Card */}
                  <div style={{ padding: '15px', backgroundColor: darkMode ? '#1e293b' : 'white', borderRadius: '10px', border: darkMode ? '1px solid #8b5cf6' : '1px solid #7c3aed', position: 'relative', overflow: 'hidden' }}>
                    <div style={{ position: 'absolute', top: '-10px', right: '-10px', fontSize: '50px', opacity: 0.1 }}>💿</div>
                    <div style={{ fontSize: '32px', fontWeight: 'bold', color: darkMode ? '#a78bfa' : '#7c3aed' }}>
                      {boxAwsEc2Instances.reduce((sum, inst) => sum + (boxAwsServiceConfigs.ec2?.instances?.[inst.id]?.root_volume_size || boxAwsEc2StorageSize || 8), 0)} <span style={{ fontSize: '16px' }}>GiB</span>
                    </div>
                    <div style={{ fontSize: '13px', color: darkMode ? '#94a3b8' : '#666', fontWeight: '500' }}>Root Storage</div>
                  </div>

                  {/* Additional EBS Card */}
                  <div style={{ padding: '15px', backgroundColor: darkMode ? '#1e293b' : 'white', borderRadius: '10px', border: darkMode ? '1px solid #f59e0b' : '1px solid #d97706', position: 'relative', overflow: 'hidden' }}>
                    <div style={{ position: 'absolute', top: '-10px', right: '-10px', fontSize: '50px', opacity: 0.1 }}>💾</div>
                    <div style={{ fontSize: '32px', fontWeight: 'bold', color: darkMode ? '#fbbf24' : '#d97706' }}>
                      {boxAwsEc2Instances.reduce((sum, inst) => sum + (inst.ebsVolumes || []).reduce((volSum, v) => volSum + (v.size || 20), 0), 0)} <span style={{ fontSize: '16px' }}>GiB</span>
                    </div>
                    <div style={{ fontSize: '13px', color: darkMode ? '#94a3b8' : '#666', fontWeight: '500' }}>Additional EBS ({boxAwsEc2Instances.reduce((count, inst) => count + (inst.ebsVolumes || []).length, 0)})</div>
                  </div>

                  {/* Total Storage Card */}
                  <div style={{ padding: '15px', backgroundColor: darkMode ? '#1e293b' : 'white', borderRadius: '10px', border: darkMode ? '1px solid #10b981' : '1px solid #059669', position: 'relative', overflow: 'hidden' }}>
                    <div style={{ position: 'absolute', top: '-10px', right: '-10px', fontSize: '50px', opacity: 0.1 }}>📦</div>
                    <div style={{ fontSize: '32px', fontWeight: 'bold', color: darkMode ? '#34d399' : '#059669' }}>
                      {boxAwsEc2Instances.reduce((sum, inst) => sum + (boxAwsServiceConfigs.ec2?.instances?.[inst.id]?.root_volume_size || boxAwsEc2StorageSize || 8), 0) + boxAwsEc2Instances.reduce((sum, inst) => sum + (inst.ebsVolumes || []).reduce((volSum, v) => volSum + (v.size || 20), 0), 0)} <span style={{ fontSize: '16px' }}>GiB</span>
                    </div>
                    <div style={{ fontSize: '13px', color: darkMode ? '#94a3b8' : '#666', fontWeight: '500' }}>Total Storage</div>
                  </div>
                </div>

                {/* Details Section */}
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: '15px' }}>
                  {/* Instance Details */}
                  <div style={{ padding: '15px', backgroundColor: darkMode ? '#1e293b' : 'white', borderRadius: '10px', border: '1px solid #ddd' }}>
                    <h4 style={{ margin: '0 0 12px 0', fontSize: '14px', fontWeight: 'bold', color: darkMode ? '#60a5fa' : '#0073bb', display: 'flex', alignItems: 'center', gap: '8px' }}>
                      🖥️ Instance Configuration
                    </h4>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', fontSize: '13px' }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: darkMode ? '1px solid #334155' : '1px solid #e5e7eb' }}>
                        <span style={{ color: darkMode ? '#94a3b8' : '#666' }}>Instance Type</span>
                        <span style={{ fontWeight: '600', color: darkMode ? '#e2e8f0' : '#333' }}>{boxAwsServiceConfigs.ec2?.instance_type || 't3.micro'}</span>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: darkMode ? '1px solid #334155' : '1px solid #e5e7eb' }}>
                        <span style={{ color: darkMode ? '#94a3b8' : '#666' }}>AMI</span>
                        <span style={{ fontWeight: '600', color: darkMode ? '#e2e8f0' : '#333', maxWidth: '150px', overflow: 'hidden', textOverflow: 'ellipsis' }}>{boxAwsEc2SelectedAmi?.id?.substring(0, 15) || 'Not selected'}...</span>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: darkMode ? '1px solid #334155' : '1px solid #e5e7eb' }}>
                        <span style={{ color: darkMode ? '#94a3b8' : '#666' }}>Key Pair</span>
                        <span style={{ fontWeight: '600', color: boxAwsServiceConfigs.ec2?.key_name ? (darkMode ? '#34d399' : '#059669') : (darkMode ? '#f87171' : '#dc2626') }}>
                          {boxAwsServiceConfigs.ec2?.key_name || '⚠️ Not configured'}
                        </span>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0' }}>
                        <span style={{ color: darkMode ? '#94a3b8' : '#666' }}>Root Volume Type</span>
                        <span style={{ fontWeight: '600', color: darkMode ? '#e2e8f0' : '#333' }}>{boxAwsEc2StorageType || 'gp3'}</span>
                      </div>
                    </div>
                  </div>

                  {/* Network & Security */}
                  <div style={{ padding: '15px', backgroundColor: darkMode ? '#1e293b' : 'white', borderRadius: '10px', border: '1px solid #ddd' }}>
                    <h4 style={{ margin: '0 0 12px 0', fontSize: '14px', fontWeight: 'bold', color: darkMode ? '#22c55e' : '#16a34a', display: 'flex', alignItems: 'center', gap: '8px' }}>
                      🔐 Network & Security
                    </h4>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', fontSize: '13px' }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: darkMode ? '1px solid #334155' : '1px solid #e5e7eb' }}>
                        <span style={{ color: darkMode ? '#94a3b8' : '#666' }}>VPC</span>
                        <span style={{ fontWeight: '600', color: boxAwsServiceConfigs.ec2?.vpc_id ? (darkMode ? '#34d399' : '#059669') : (darkMode ? '#f87171' : '#dc2626') }}>
                          {boxAwsServiceConfigs.ec2?.vpc_id ? '✓ Configured' : '⚠️ Select VPC'}
                        </span>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: darkMode ? '1px solid #334155' : '1px solid #e5e7eb' }}>
                        <span style={{ color: darkMode ? '#94a3b8' : '#666' }}>Security Group</span>
                        <span style={{ fontWeight: '600', color: darkMode ? '#34d399' : '#059669' }}>✓ Auto-created</span>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: darkMode ? '1px solid #334155' : '1px solid #e5e7eb' }}>
                        <span style={{ color: darkMode ? '#94a3b8' : '#666' }}>SSH (Port 22)</span>
                        <span style={{ fontWeight: '600', color: darkMode ? '#34d399' : '#059669' }}>✓ Enabled</span>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0' }}>
                        <span style={{ color: darkMode ? '#94a3b8' : '#666' }}>HTTP/HTTPS</span>
                        <span style={{ fontWeight: '600', color: darkMode ? '#34d399' : '#059669' }}>✓ Enabled</span>
                      </div>
                    </div>
                  </div>
                </div>

                {/* Instance List */}
                {boxAwsEc2Instances.length > 0 && (
                  <div style={{ marginTop: '15px', padding: '15px', backgroundColor: darkMode ? '#1e293b' : 'white', borderRadius: '10px', border: '1px solid #ddd' }}>
                    <h4 style={{ margin: '0 0 12px 0', fontSize: '14px', fontWeight: 'bold', color: darkMode ? '#a78bfa' : '#7c3aed' }}>
                      📋 Instances to Create ({boxAwsEc2Instances.length})
                    </h4>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
                      {boxAwsEc2Instances.map((inst, idx) => (
                        <div key={inst.id} style={{ 
                          padding: '8px 12px', 
                          backgroundColor: darkMode ? '#334155' : '#f3f4f6', 
                          borderRadius: '6px',
                          fontSize: '12px',
                          display: 'flex',
                          alignItems: 'center',
                          gap: '8px'
                        }}>
                          <span style={{ 
                            backgroundColor: darkMode ? '#60a5fa' : '#0073bb', 
                            color: 'white', 
                            padding: '2px 6px', 
                            borderRadius: '4px',
                            fontSize: '10px',
                            fontWeight: 'bold'
                          }}>
                            #{idx + 1}
                          </span>
                          <span style={{ fontWeight: '600', color: darkMode ? '#e2e8f0' : '#333' }}>
                            {boxAwsServiceConfigs.ec2?.instances?.[inst.id]?.name || inst.name || `instance-${inst.id}`}
                          </span>
                          <span style={{ color: darkMode ? '#94a3b8' : '#666' }}>
                            ({boxAwsServiceConfigs.ec2?.instances?.[inst.id]?.root_volume_size || boxAwsEc2StorageSize || 8} GiB)
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* EBS Volumes List */}
                {(() => {
                  const totalVolumes = boxAwsEc2Instances.reduce((count, inst) => count + (inst.ebsVolumes || []).length, 0);
                  return totalVolumes > 0 && (
                    <div style={{ marginTop: '10px', padding: '15px', backgroundColor: darkMode ? '#1e293b' : 'white', borderRadius: '10px', border: '1px solid #ddd' }}>
                      <h4 style={{ margin: '0 0 12px 0', fontSize: '14px', fontWeight: 'bold', color: darkMode ? '#fbbf24' : '#d97706' }}>
                        💾 Additional Volumes ({totalVolumes})
                      </h4>
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
                        {boxAwsEc2Instances.flatMap(inst => 
                          (inst.ebsVolumes || []).map(vol => ({ ...vol, instanceName: inst.name }))
                        ).map((vol, idx) => (
                          <div key={`${vol.instanceName}-${vol.id}`} style={{ 
                            padding: '8px 12px', 
                            backgroundColor: darkMode ? '#334155' : '#fef3c7', 
                            borderRadius: '6px',
                            fontSize: '12px',
                            display: 'flex',
                            alignItems: 'center',
                            gap: '8px'
                          }}>
                            <span style={{ fontWeight: '600', color: darkMode ? '#fbbf24' : '#92400e' }}>
                              {vol.name || `volume-${vol.id}`}
                            </span>
                            <span style={{ color: darkMode ? '#94a3b8' : '#78350f' }}>
                              {vol.size || 20} GiB • {vol.type || 'gp3'}
                            </span>
                            <span style={{ 
                              backgroundColor: darkMode ? '#1e40af' : '#dbeafe', 
                              color: darkMode ? '#93c5fd' : '#1e40af',
                              padding: '2px 6px', 
                              borderRadius: '4px',
                              fontSize: '10px'
                            }}>
                              🔗 {vol.instanceName || 'Instance'}
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>
                  );
                })()}

                {/* Cost Estimate Hint */}
                <div style={{ 
                  marginTop: '15px', 
                  padding: '12px 15px', 
                  backgroundColor: darkMode ? '#172554' : '#eff6ff', 
                  borderRadius: '8px',
                  border: darkMode ? '1px solid #1e40af' : '1px solid #3b82f6',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '10px'
                }}>
                  <span style={{ fontSize: '20px' }}>💡</span>
                  <div style={{ fontSize: '13px', color: darkMode ? '#93c5fd' : '#1e40af' }}>
                    <strong>Tip:</strong> After deployment, use <code style={{ backgroundColor: darkMode ? '#1e3a5f' : '#dbeafe', padding: '2px 6px', borderRadius: '4px' }}>terraform plan</code> to review resources and estimated costs before applying.
                  </div>
                </div>
              </div>
              </div>
              )}
            </div>
          )}
          {/* VPC Configuration - removed duplicate, VPC is already shown above */}
          {boxAwsSelectedServices.includes('s3') && (
            <div className="service-input-card">
              <div style={{ borderBottom: '1px solid #ddd', paddingBottom: '10px', marginBottom: '20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div style={{ flex: 1 }}>
                  <h2 style={{ margin: 0, fontSize: '20px', fontWeight: 'bold' }}>🪣 S3 Configuration</h2>
                  <p style={{ margin: '5px 0 0 0', color: '#666', fontSize: '14px' }}>
                    Amazon S3 provides object storage with 99.999999999% durability.
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => setBoxAwsServiceExpanded({ ...boxAwsServiceExpanded, s3: !boxAwsServiceExpanded.s3 })}
                  style={{
                    background: 'none',
                    border: 'none',
                    cursor: 'pointer',
                    padding: '5px 10px',
                    fontSize: '18px',
                    color: '#666',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    minWidth: '30px',
                    height: '30px'
                  }}
                  title={boxAwsServiceExpanded.s3 ? 'Collapse' : 'Expand'}
                >
                  <span style={{ 
                    transform: boxAwsServiceExpanded.s3 ? 'rotate(180deg)' : 'rotate(0deg)',
                    transition: 'transform 0.2s',
                    display: 'inline-block'
                  }}>
                    ▼
                  </span>
                </button>
              </div>
              {boxAwsServiceExpanded.s3 && (
                <div>
                  {/* Number of S3 Buckets */}
                  <label style={{ marginBottom: '20px', display: 'block' }}>
                    <span style={{ fontWeight: 'bold', fontSize: '14px' }}>Number of S3 Buckets</span>
                    <input
                      type="number"
                      min="1"
                      max="10"
                      value={boxAwsS3Count}
                      onChange={(e) => {
                        const count = parseInt(e.target.value) || 1;
                        setBoxAwsS3Count(Math.min(Math.max(count, 1), 10));
                        // Initialize buckets array if needed
                        const currentBuckets = boxAwsServiceConfigs.s3?.buckets || [];
                        if (count > currentBuckets.length) {
                          const newBuckets = [...currentBuckets];
                          for (let i = currentBuckets.length; i < count; i++) {
                            newBuckets.push({ bucket_name: '', storage_class: 'STANDARD', versioning: true, encryption: true, block_public_access: true });
                          }
                          setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, s3: { ...boxAwsServiceConfigs.s3, buckets: newBuckets } });
                        }
                      }}
                      style={{ width: '80px', padding: '8px', marginLeft: '10px' }}
                    />
                    <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                      Create up to 10 S3 buckets at once
                    </small>
                  </label>

                  {/* Individual Bucket Configurations */}
                  {Array.from({ length: boxAwsS3Count }).map((_, bucketIdx) => {
                    const bucket = boxAwsServiceConfigs.s3?.buckets?.[bucketIdx] || { bucket_name: '', storage_class: 'STANDARD', versioning: true, encryption: true, block_public_access: true };
                    
                    // Initialize expand state for this bucket
                    if (boxAwsS3BucketsExpanded[bucketIdx] === undefined) {
                      boxAwsS3BucketsExpanded[bucketIdx] = true;
                    }
                    
                    return (
                      <div 
                        key={bucketIdx} 
                        style={{ 
                          marginBottom: '20px', 
                          border: darkMode ? '2px solid #f97316' : '2px solid #ea580c', 
                          borderRadius: '8px',
                          overflow: 'hidden'
                        }}
                      >
                        {/* Bucket Header */}
                        <div 
                          style={{ 
                            padding: '12px 15px', 
                            backgroundColor: darkMode ? '#7c2d12' : '#ea580c',
                            color: 'white',
                            display: 'flex',
                            justifyContent: 'space-between',
                            alignItems: 'center',
                            cursor: 'pointer'
                          }}
                          onClick={() => setBoxAwsS3BucketsExpanded({ 
                            ...boxAwsS3BucketsExpanded, 
                            [bucketIdx]: !boxAwsS3BucketsExpanded[bucketIdx] 
                          })}
                        >
                          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                            <span style={{ 
                              backgroundColor: 'rgba(255,255,255,0.2)', 
                              padding: '4px 10px', 
                              borderRadius: '4px',
                              fontSize: '14px',
                              fontWeight: 'bold'
                            }}>
                              #{bucketIdx + 1}
                            </span>
                            <span style={{ fontWeight: 'bold', fontSize: '16px' }}>
                              🪣 {bucket.bucket_name || `Bucket ${bucketIdx + 1}`}
                            </span>
                          </div>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                            {boxAwsS3Count > 1 && (
                              <button
                                type="button"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  const newBuckets = [...(boxAwsServiceConfigs.s3?.buckets || [])];
                                  newBuckets.splice(bucketIdx, 1);
                                  setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, s3: { ...boxAwsServiceConfigs.s3, buckets: newBuckets } });
                                  setBoxAwsS3Count(Math.max(1, boxAwsS3Count - 1));
                                }}
                                style={{
                                  background: 'rgba(255,255,255,0.2)',
                                  border: 'none',
                                  color: 'white',
                                  padding: '4px 8px',
                                  borderRadius: '4px',
                                  cursor: 'pointer',
                                  fontSize: '12px'
                                }}
                                title="Remove this bucket"
                              >
                                🗑️ Remove
                              </button>
                            )}
                            <span style={{ 
                              transform: boxAwsS3BucketsExpanded[bucketIdx] ? 'rotate(180deg)' : 'rotate(0deg)',
                              transition: 'transform 0.2s',
                              display: 'inline-block'
                            }}>
                              ▼
                            </span>
                          </div>
                        </div>

                        {/* Bucket Configuration (Expandable) */}
                        {boxAwsS3BucketsExpanded[bucketIdx] && (
                          <div style={{ padding: '15px', backgroundColor: darkMode ? '#1e293b' : '#fff7ed' }}>
                        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(250px, 1fr))', gap: '15px' }}>
                          <label style={{ display: 'block' }}>
                            <span style={{ fontSize: '13px', fontWeight: '600', color: darkMode ? '#e2e8f0' : '#333' }}>Bucket Name</span>
                            <input
                              value={bucket.bucket_name || ''}
                              onChange={(e) => {
                                const newBuckets = [...(boxAwsServiceConfigs.s3?.buckets || [])];
                                newBuckets[bucketIdx] = { ...bucket, bucket_name: e.target.value.toLowerCase().replace(/[^a-z0-9.-]/g, '-') };
                                setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, s3: { ...boxAwsServiceConfigs.s3, buckets: newBuckets } });
                              }}
                              placeholder={`my-bucket-${bucketIdx + 1}`}
                              style={{ width: '100%', padding: '8px', marginTop: '5px', fontSize: '14px' }}
                            />
                            <small style={{ color: '#666', display: 'block', marginTop: '4px' }}>Globally unique name</small>
                          </label>
                          <label style={{ display: 'block' }}>
                            <span style={{ fontSize: '13px', fontWeight: '600', color: darkMode ? '#e2e8f0' : '#333' }}>Storage Class</span>
                            <select
                              value={bucket.storage_class || 'STANDARD'}
                              onChange={(e) => {
                                const newBuckets = [...(boxAwsServiceConfigs.s3?.buckets || [])];
                                newBuckets[bucketIdx] = { ...bucket, storage_class: e.target.value };
                                setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, s3: { ...boxAwsServiceConfigs.s3, buckets: newBuckets } });
                              }}
                              style={{ width: '100%', padding: '8px', marginTop: '5px', fontSize: '14px' }}
                            >
                              <option value="STANDARD">Standard</option>
                              <option value="STANDARD_IA">Standard-IA</option>
                              <option value="INTELLIGENT_TIERING">Intelligent-Tiering</option>
                              <option value="GLACIER">Glacier</option>
                            </select>
                          </label>
                        </div>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '20px', marginTop: '15px' }}>
                          <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer' }}>
                            <input type="checkbox" checked={bucket.versioning !== false} onChange={(e) => {
                              const newBuckets = [...(boxAwsServiceConfigs.s3?.buckets || [])];
                              newBuckets[bucketIdx] = { ...bucket, versioning: e.target.checked };
                              setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, s3: { ...boxAwsServiceConfigs.s3, buckets: newBuckets } });
                            }} style={{ width: '16px', height: '16px' }} />
                            <span style={{ fontSize: '13px', color: darkMode ? '#e2e8f0' : '#333' }}>📦 Versioning</span>
                          </label>
                          <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer' }}>
                            <input type="checkbox" checked={bucket.encryption !== false} onChange={(e) => {
                              const newBuckets = [...(boxAwsServiceConfigs.s3?.buckets || [])];
                              newBuckets[bucketIdx] = { ...bucket, encryption: e.target.checked };
                              setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, s3: { ...boxAwsServiceConfigs.s3, buckets: newBuckets } });
                            }} style={{ width: '16px', height: '16px' }} />
                            <span style={{ fontSize: '13px', color: darkMode ? '#e2e8f0' : '#333' }}>🔒 Encryption</span>
                          </label>
                          <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer' }}>
                            <input type="checkbox" checked={bucket.block_public_access !== false} onChange={(e) => {
                              const newBuckets = [...(boxAwsServiceConfigs.s3?.buckets || [])];
                              newBuckets[bucketIdx] = { ...bucket, block_public_access: e.target.checked };
                              setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, s3: { ...boxAwsServiceConfigs.s3, buckets: newBuckets } });
                            }} style={{ width: '16px', height: '16px' }} />
                            <span style={{ fontSize: '13px', color: darkMode ? '#e2e8f0' : '#333' }}>🚫 Block Public</span>
                          </label>
                          <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer' }}>
                            <input type="checkbox" checked={bucket.enable_logging !== false} onChange={(e) => {
                              const newBuckets = [...(boxAwsServiceConfigs.s3?.buckets || [])];
                              newBuckets[bucketIdx] = { ...bucket, enable_logging: e.target.checked };
                              setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, s3: { ...boxAwsServiceConfigs.s3, buckets: newBuckets } });
                            }} style={{ width: '16px', height: '16px' }} />
                            <span style={{ fontSize: '13px', color: darkMode ? '#e2e8f0' : '#333' }}>📋 Access Logs</span>
                          </label>
                        </div>

                        {/* Lifecycle Policy */}
                        <h5 style={{ marginTop: '20px', marginBottom: '10px', fontSize: '14px', fontWeight: 'bold', color: darkMode ? '#fb923c' : '#c2410c' }}>
                          ⏰ Lifecycle Policy
                        </h5>
                        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(250px, 1fr))', gap: '15px' }}>
                  <label>
                            <span style={{ fontSize: '13px', fontWeight: '600' }}>Transition to IA (days)</span>
                    <input
                              type="number"
                              min="30"
                              value={bucket.lifecycle_ia_days || ''}
                      onChange={(e) => {
                                const newBuckets = [...(boxAwsServiceConfigs.s3?.buckets || [])];
                                newBuckets[bucketIdx] = { ...bucket, lifecycle_ia_days: parseInt(e.target.value) || null };
                                setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, s3: { ...boxAwsServiceConfigs.s3, buckets: newBuckets } });
                              }}
                              placeholder="30"
                              style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                            />
                            <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                              Move to Standard-IA after X days
                            </small>
                          </label>
                          <label>
                            <span style={{ fontSize: '13px', fontWeight: '600' }}>Transition to Glacier (days)</span>
                            <input
                              type="number"
                              min="90"
                              value={bucket.lifecycle_glacier_days || ''}
                              onChange={(e) => {
                                const newBuckets = [...(boxAwsServiceConfigs.s3?.buckets || [])];
                                newBuckets[bucketIdx] = { ...bucket, lifecycle_glacier_days: parseInt(e.target.value) || null };
                                setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, s3: { ...boxAwsServiceConfigs.s3, buckets: newBuckets } });
                      }}
                              placeholder="90"
                              style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                            />
                            <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                              Archive to Glacier after X days
                            </small>
                          </label>
                          <label>
                            <span style={{ fontSize: '13px', fontWeight: '600' }}>Expiration (days)</span>
                            <input
                              type="number"
                              min="1"
                              value={bucket.lifecycle_expiration_days || ''}
                              onChange={(e) => {
                                const newBuckets = [...(boxAwsServiceConfigs.s3?.buckets || [])];
                                newBuckets[bucketIdx] = { ...bucket, lifecycle_expiration_days: parseInt(e.target.value) || null };
                                setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, s3: { ...boxAwsServiceConfigs.s3, buckets: newBuckets } });
                              }}
                              placeholder="365"
                              style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                            />
                            <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                              Delete objects after X days
                            </small>
                  </label>
                        </div>

                        {/* CORS & Tags */}
                        <h5 style={{ marginTop: '20px', marginBottom: '10px', fontSize: '14px', fontWeight: 'bold', color: darkMode ? '#fb923c' : '#c2410c' }}>
                          🔧 Advanced Configuration
                        </h5>
                        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(250px, 1fr))', gap: '15px' }}>
                          <label style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                            <input type="checkbox" checked={bucket.enable_cors !== false} onChange={(e) => {
                              const newBuckets = [...(boxAwsServiceConfigs.s3?.buckets || [])];
                              newBuckets[bucketIdx] = { ...bucket, enable_cors: e.target.checked };
                              setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, s3: { ...boxAwsServiceConfigs.s3, buckets: newBuckets } });
                            }} style={{ width: '16px', height: '16px' }} />
                            <span style={{ fontSize: '13px', color: darkMode ? '#e2e8f0' : '#333' }}>Enable CORS (Cross-Origin Resource Sharing)</span>
                          </label>
                          <label>
                            <span style={{ fontSize: '13px', fontWeight: '600' }}>Tags (comma-separated key=value)</span>
                            <input
                              value={bucket.tags || ''}
                              onChange={(e) => {
                                const newBuckets = [...(boxAwsServiceConfigs.s3?.buckets || [])];
                                newBuckets[bucketIdx] = { ...bucket, tags: e.target.value };
                                setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, s3: { ...boxAwsServiceConfigs.s3, buckets: newBuckets } });
                              }}
                              placeholder="Environment=Production,Owner=DevTeam"
                              style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                            />
                            <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                              Example: Env=prod,Owner=team
                            </small>
                          </label>
                        </div>
                          </div>
                        )}
                      </div>
                    );
                  })}

                  {/* S3 Summary */}
                  <div style={{ marginTop: '25px', padding: '20px', backgroundColor: darkMode ? '#0f172a' : '#fff7ed', borderRadius: '12px', border: darkMode ? '2px solid #f97316' : '2px solid #ea580c' }}>
                    <h3 style={{ margin: '0 0 15px 0', fontSize: '16px', fontWeight: 'bold', color: darkMode ? '#fb923c' : '#c2410c', display: 'flex', alignItems: 'center', gap: '10px' }}>
                      📊 S3 Configuration Summary
                      <span style={{ fontSize: '11px', fontWeight: 'normal', backgroundColor: '#22c55e', color: 'white', padding: '3px 8px', borderRadius: '10px' }}>
                        {boxAwsS3Count} Bucket{boxAwsS3Count > 1 ? 's' : ''}
                      </span>
                    </h3>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px' }}>
                      {Array.from({ length: boxAwsS3Count }).map((_, idx) => {
                        const bucket = boxAwsServiceConfigs.s3?.buckets?.[idx] || {};
                        return (
                          <div key={idx} style={{ padding: '12px', backgroundColor: darkMode ? '#1e293b' : 'white', borderRadius: '8px', border: '1px solid #ddd', minWidth: '200px' }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
                              <span style={{ backgroundColor: darkMode ? '#f97316' : '#ea580c', color: 'white', padding: '2px 8px', borderRadius: '4px', fontSize: '11px', fontWeight: 'bold' }}>#{idx + 1}</span>
                              <span style={{ fontSize: '13px', fontWeight: 'bold', color: darkMode ? '#e2e8f0' : '#333', wordBreak: 'break-all' }}>{bucket.bucket_name || 'Not named'}</span>
                            </div>
                            <div style={{ display: 'flex', gap: '5px', flexWrap: 'wrap' }}>
                              <span style={{ fontSize: '10px', padding: '2px 6px', borderRadius: '4px', backgroundColor: darkMode ? '#334155' : '#f3f4f6' }}>{bucket.storage_class || 'STANDARD'}</span>
                              {bucket.versioning !== false && <span style={{ fontSize: '10px', padding: '2px 6px', borderRadius: '4px', backgroundColor: '#dcfce7', color: '#166534' }}>✓ Ver</span>}
                              {bucket.encryption !== false && <span style={{ fontSize: '10px', padding: '2px 6px', borderRadius: '4px', backgroundColor: '#dbeafe', color: '#1e40af' }}>🔒</span>}
                              {bucket.block_public_access !== false && <span style={{ fontSize: '10px', padding: '2px 6px', borderRadius: '4px', backgroundColor: '#fef3c7', color: '#92400e' }}>🛡️</span>}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
          {boxAwsSelectedServices.includes('rds') && (
            <div className="service-input-card">
              <div style={{ borderBottom: '1px solid #ddd', paddingBottom: '10px', marginBottom: '20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div style={{ flex: 1 }}>
                  <h2 style={{ margin: 0, fontSize: '20px', fontWeight: 'bold' }}>🗄️ RDS Configuration</h2>
                  <p style={{ margin: '5px 0 0 0', color: '#666', fontSize: '14px' }}>
                    Amazon RDS provides managed relational database services with automated backups.
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => setBoxAwsServiceExpanded({ ...boxAwsServiceExpanded, rds: !boxAwsServiceExpanded.rds })}
                  style={{
                    background: 'none',
                    border: 'none',
                    cursor: 'pointer',
                    padding: '5px 10px',
                    fontSize: '18px',
                    color: '#666',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    minWidth: '30px',
                    height: '30px'
                  }}
                  title={boxAwsServiceExpanded.rds ? 'Collapse' : 'Expand'}
                >
                  <span style={{ 
                    transform: boxAwsServiceExpanded.rds ? 'rotate(180deg)' : 'rotate(0deg)',
                    transition: 'transform 0.2s',
                    display: 'inline-block'
                  }}>
                    ▼
                  </span>
                </button>
              </div>
              {boxAwsServiceExpanded.rds && (
                <div>
              {/* Number of RDS Databases */}
              <label style={{ marginBottom: '20px', display: 'block' }}>
                <span style={{ fontWeight: 'bold', fontSize: '14px' }}>Number of RDS Databases</span>
                <input
                  type="number"
                  min="1"
                  max="5"
                  value={boxAwsRdsCount}
                  onChange={(e) => {
                    const count = parseInt(e.target.value) || 1;
                    setBoxAwsRdsCount(Math.min(Math.max(count, 1), 5));
                    // Initialize databases array if needed
                    const currentDbs = boxAwsServiceConfigs.rds?.databases || [];
                    if (count > currentDbs.length) {
                      const newDbs = [...currentDbs];
                      for (let i = currentDbs.length; i < count; i++) {
                        newDbs.push({ identifier: `database-${i + 1}`, engine: 'mysql', instance_class: '', db_name: '', username: 'admin', password: '', allocated_storage: 20, storage_type: 'gp3', backup_retention_period: 7 });
                      }
                      setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, rds: { ...boxAwsServiceConfigs.rds, databases: newDbs } });
                    }
                  }}
                  style={{ width: '80px', padding: '8px', marginLeft: '10px' }}
                />
                <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                  Create up to 5 RDS database instances at once
                </small>
              </label>

              {boxAwsRdsDataLoading && <small>Loading RDS data...</small>}

              {/* Individual Database Configurations */}
              {Array.from({ length: boxAwsRdsCount }).map((_, dbIdx) => {
                const db = boxAwsServiceConfigs.rds?.databases?.[dbIdx] || { identifier: `database-${dbIdx + 1}`, engine: 'mysql', instance_class: '', db_name: '', username: 'admin', password: '', allocated_storage: 20, storage_type: 'gp3', backup_retention_period: 7 };
                
                // Helper function to format engine names
                const getEngineName = (engine) => {
                  const engineMap = {
                    'mysql': 'MySQL',
                    'postgres': 'PostgreSQL',
                    'mariadb': 'MariaDB',
                    'aurora-mysql': 'Aurora (MySQL)',
                    'aurora-postgresql': 'Aurora (PostgreSQL)',
                    'oracle-ee': 'Oracle Enterprise Edition',
                    'oracle-ee-cdb': 'Oracle EE (CDB)',
                    'oracle-se2': 'Oracle Standard Edition 2',
                    'oracle-se2-cdb': 'Oracle SE2 (CDB)',
                    'sqlserver-ee': 'SQL Server Enterprise',
                    'sqlserver-se': 'SQL Server Standard',
                    'sqlserver-ex': 'SQL Server Express',
                    'sqlserver-web': 'SQL Server Web',
                    'custom-oracle-ee': 'Custom Oracle EE',
                    'custom-sqlserver-ee': 'Custom SQL Server EE',
                    'custom-sqlserver-se': 'Custom SQL Server SE',
                    'custom-sqlserver-web': 'Custom SQL Server Web',
                    'docdb': 'DocumentDB'
                  };
                  return engineMap[engine] || engine;
                };
                
                // Initialize expand state for this database
                if (boxAwsRdsDatabasesExpanded[dbIdx] === undefined) {
                  boxAwsRdsDatabasesExpanded[dbIdx] = true;
                }
                
                return (
                  <div 
                    key={dbIdx} 
                    style={{ 
                      marginBottom: '20px', 
                      border: darkMode ? '2px solid #3b82f6' : '2px solid #2563eb', 
                      borderRadius: '8px',
                      overflow: 'hidden'
                    }}
                  >
                    {/* Database Header */}
                    <div 
                      style={{ 
                        padding: '12px 15px', 
                        backgroundColor: darkMode ? '#1e3a8a' : '#2563eb',
                        color: 'white',
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        cursor: 'pointer'
                      }}
                      onClick={() => setBoxAwsRdsDatabasesExpanded({ 
                        ...boxAwsRdsDatabasesExpanded, 
                        [dbIdx]: !boxAwsRdsDatabasesExpanded[dbIdx] 
                      })}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                        <span style={{ 
                          backgroundColor: 'rgba(255,255,255,0.2)', 
                          padding: '4px 10px', 
                          borderRadius: '4px',
                          fontSize: '14px',
                          fontWeight: 'bold'
                        }}>
                          #{dbIdx + 1}
                        </span>
                        <span style={{ fontWeight: 'bold', fontSize: '16px' }}>
                          🗄️ {db.identifier || `Database ${dbIdx + 1}`}
                        </span>
                        <span style={{ fontSize: '11px', fontWeight: 'normal', backgroundColor: db.engine === 'mysql' ? '#00758f' : db.engine === 'postgres' ? '#336791' : 'rgba(255,255,255,0.3)', color: 'white', padding: '2px 8px', borderRadius: '4px' }}>
                          {getEngineName(db.engine || 'mysql')}
                        </span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                        {boxAwsRdsCount > 1 && (
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation();
                              const newDbs = [...(boxAwsServiceConfigs.rds?.databases || [])];
                              newDbs.splice(dbIdx, 1);
                              setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, rds: { ...boxAwsServiceConfigs.rds, databases: newDbs } });
                              setBoxAwsRdsCount(Math.max(1, boxAwsRdsCount - 1));
                            }}
                            style={{
                              background: 'rgba(255,255,255,0.2)',
                              border: 'none',
                              color: 'white',
                              padding: '4px 8px',
                              borderRadius: '4px',
                              cursor: 'pointer',
                              fontSize: '12px'
                            }}
                            title="Remove this database"
                          >
                            🗑️ Remove
                          </button>
                        )}
                        <span style={{ 
                          transform: boxAwsRdsDatabasesExpanded[dbIdx] ? 'rotate(180deg)' : 'rotate(0deg)',
                          transition: 'transform 0.2s',
                          display: 'inline-block'
                        }}>
                          ▼
                        </span>
                      </div>
                    </div>

                    {/* Database Configuration (Expandable) */}
                    {boxAwsRdsDatabasesExpanded[dbIdx] && (
                      <div style={{ padding: '20px', backgroundColor: darkMode ? '#1e293b' : '#eff6ff' }}>

                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '15px' }}>
                      <label>
                        <span style={{ fontSize: '13px', fontWeight: '600' }}>Database Identifier</span>
                        <input
                          value={db.identifier || ''}
                          onChange={(e) => {
                            const newDbs = [...(boxAwsServiceConfigs.rds?.databases || [])];
                            newDbs[dbIdx] = { ...db, identifier: e.target.value };
                            setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, rds: { ...boxAwsServiceConfigs.rds, databases: newDbs } });
                          }}
                          placeholder={`database-${dbIdx + 1}`}
                          style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                        />
                      </label>
                      <label>
                        <span style={{ fontSize: '13px', fontWeight: '600' }}>Database Engine</span>
                        <select
                          value={db.engine || 'mysql'}
                          onChange={(e) => {
                            const newDbs = [...(boxAwsServiceConfigs.rds?.databases || [])];
                            newDbs[dbIdx] = { ...db, engine: e.target.value, instance_class: '' };
                            setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, rds: { ...boxAwsServiceConfigs.rds, databases: newDbs } });
                          }}
                          disabled={boxAwsRdsDataLoading}
                          style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                        >
                          {boxAwsRdsData.engines.length > 0 ? boxAwsRdsData.engines.map((engine) => (
                            <option key={engine} value={engine}>{getEngineName(engine)}</option>
                          )) : (
                            <>
                              <option value="mysql">MySQL</option>
                              <option value="postgres">PostgreSQL</option>
                              <option value="mariadb">MariaDB</option>
                              <option value="aurora-mysql">Aurora (MySQL)</option>
                              <option value="aurora-postgresql">Aurora (PostgreSQL)</option>
                              <option value="oracle-ee">Oracle Enterprise Edition</option>
                              <option value="oracle-se2">Oracle Standard Edition 2</option>
                              <option value="sqlserver-ee">SQL Server Enterprise</option>
                              <option value="sqlserver-se">SQL Server Standard</option>
                              <option value="sqlserver-ex">SQL Server Express</option>
                              <option value="sqlserver-web">SQL Server Web</option>
                            </>
                          )}
                        </select>
                      </label>
                      <label>
                        <span style={{ fontSize: '13px', fontWeight: '600' }}>Instance Class</span>
                        <select
                          value={db.instance_class || ''}
                          onChange={(e) => {
                            const newDbs = [...(boxAwsServiceConfigs.rds?.databases || [])];
                            newDbs[dbIdx] = { ...db, instance_class: e.target.value };
                            setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, rds: { ...boxAwsServiceConfigs.rds, databases: newDbs } });
                          }}
                          disabled={boxAwsRdsDataLoading}
                          style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                        >
                          <option value="">Select instance class...</option>
                          {boxAwsRdsData.instance_classes.length > 0 ? boxAwsRdsData.instance_classes.map((cls) => (
                            <option key={cls} value={cls}>{cls}</option>
                          )) : (
                            <>
                              <option value="db.t3.micro">db.t3.micro</option>
                              <option value="db.t3.small">db.t3.small</option>
                              <option value="db.t3.medium">db.t3.medium</option>
                              <option value="db.m5.large">db.m5.large</option>
                            </>
                          )}
                        </select>
                      </label>
                      <label>
                        <span style={{ fontSize: '13px', fontWeight: '600' }}>Database Name</span>
                        <input
                          value={db.db_name || ''}
                          onChange={(e) => {
                            const newDbs = [...(boxAwsServiceConfigs.rds?.databases || [])];
                            newDbs[dbIdx] = { ...db, db_name: e.target.value };
                            setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, rds: { ...boxAwsServiceConfigs.rds, databases: newDbs } });
                          }}
                          placeholder="mydb"
                          style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                        />
                      </label>
                      <label>
                        <span style={{ fontSize: '13px', fontWeight: '600' }}>Master Username</span>
                        <input
                          value={db.username || 'admin'}
                          onChange={(e) => {
                            const newDbs = [...(boxAwsServiceConfigs.rds?.databases || [])];
                            newDbs[dbIdx] = { ...db, username: e.target.value };
                            setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, rds: { ...boxAwsServiceConfigs.rds, databases: newDbs } });
                          }}
                          style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                        />
                      </label>
                      <label>
                        <span style={{ fontSize: '13px', fontWeight: '600' }}>Master Password</span>
                        <input
                          type="password"
                          value={db.password || ''}
                          onChange={(e) => {
                            const newDbs = [...(boxAwsServiceConfigs.rds?.databases || [])];
                            newDbs[dbIdx] = { ...db, password: e.target.value };
                            setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, rds: { ...boxAwsServiceConfigs.rds, databases: newDbs } });
                          }}
                          placeholder="Enter password"
                          style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                        />
                      </label>
                      <label>
                        <span style={{ fontSize: '13px', fontWeight: '600' }}>Storage (GB)</span>
                        <input
                          type="number"
                          min="20"
                          value={db.allocated_storage || 20}
                          onChange={(e) => {
                            const newDbs = [...(boxAwsServiceConfigs.rds?.databases || [])];
                            newDbs[dbIdx] = { ...db, allocated_storage: parseInt(e.target.value) || 20 };
                            setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, rds: { ...boxAwsServiceConfigs.rds, databases: newDbs } });
                          }}
                          style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                        />
                      </label>
                      <label>
                        <span style={{ fontSize: '13px', fontWeight: '600' }}>Storage Type</span>
                        <select
                          value={db.storage_type || 'gp3'}
                          onChange={(e) => {
                            const newDbs = [...(boxAwsServiceConfigs.rds?.databases || [])];
                            newDbs[dbIdx] = { ...db, storage_type: e.target.value };
                            setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, rds: { ...boxAwsServiceConfigs.rds, databases: newDbs } });
                          }}
                          style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                        >
                          <option value="gp3">gp3 (SSD)</option>
                          <option value="gp2">gp2 (SSD)</option>
                          <option value="io1">io1 (Provisioned IOPS)</option>
                        </select>
                      </label>
                    </div>

                    {/* VPC & Networking Configuration */}
                    {boxAwsSelectedServices.includes('vpc') && (
                      <>
                        <h5 style={{ marginTop: '20px', marginBottom: '10px', fontSize: '14px', fontWeight: 'bold', color: darkMode ? '#60a5fa' : '#1d4ed8' }}>
                          🌐 Network & Security
                        </h5>
                        <div className="info-callout" style={{ marginBottom: '15px', padding: '10px', backgroundColor: darkMode ? '#1e293b' : '#e0f2fe', borderRadius: '4px', fontSize: '13px' }}>
                          <strong>Note:</strong> RDS will use subnets from the VPC configuration. Select at least 2 private subnets in different AZs for Multi-AZ deployments.
                        </div>
                        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(250px, 1fr))', gap: '15px' }}>
                          <label>
                            <span style={{ fontSize: '13px', fontWeight: '600' }}>Security Group Name</span>
                            <input
                              value={db.security_group_name || ''}
                              onChange={(e) => {
                                const newDbs = [...(boxAwsServiceConfigs.rds?.databases || [])];
                                newDbs[dbIdx] = { ...db, security_group_name: e.target.value };
                                setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, rds: { ...boxAwsServiceConfigs.rds, databases: newDbs } });
                              }}
                              placeholder={`${db.identifier || 'db'}-sg`}
                              style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                            />
                            <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                              Security group will allow database port from VPC CIDR
                            </small>
                          </label>
                          <label style={{ display: 'flex', alignItems: 'center', gap: '8px', marginTop: '20px' }}>
                            <input type="checkbox" checked={db.publicly_accessible === true} onChange={(e) => {
                              const newDbs = [...(boxAwsServiceConfigs.rds?.databases || [])];
                              newDbs[dbIdx] = { ...db, publicly_accessible: e.target.checked };
                              setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, rds: { ...boxAwsServiceConfigs.rds, databases: newDbs } });
                            }} style={{ width: '16px', height: '16px' }} />
                            <span style={{ fontSize: '13px', color: darkMode ? '#e2e8f0' : '#333' }}>🌍 Publicly Accessible</span>
                          </label>
                        </div>
                      </>
                    )}

                    {/* High Availability */}
                    <h5 style={{ marginTop: '20px', marginBottom: '10px', fontSize: '14px', fontWeight: 'bold', color: darkMode ? '#60a5fa' : '#1d4ed8' }}>
                      🔄 High Availability & Backup
                    </h5>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(250px, 1fr))', gap: '15px' }}>
                      <label style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <input type="checkbox" checked={db.multi_az === true} onChange={(e) => {
                          const newDbs = [...(boxAwsServiceConfigs.rds?.databases || [])];
                          newDbs[dbIdx] = { ...db, multi_az: e.target.checked };
                          setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, rds: { ...boxAwsServiceConfigs.rds, databases: newDbs } });
                        }} style={{ width: '16px', height: '16px' }} />
                        <span style={{ fontSize: '13px', color: darkMode ? '#e2e8f0' : '#333' }}>Enable Multi-AZ Deployment</span>
                      </label>
                      <label>
                        <span style={{ fontSize: '13px', fontWeight: '600' }}>Backup Retention (days)</span>
                        <input
                          type="number"
                          min="0"
                          max="35"
                          value={db.backup_retention_period || 7}
                          onChange={(e) => {
                            const newDbs = [...(boxAwsServiceConfigs.rds?.databases || [])];
                            newDbs[dbIdx] = { ...db, backup_retention_period: parseInt(e.target.value) || 7 };
                            setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, rds: { ...boxAwsServiceConfigs.rds, databases: newDbs } });
                          }}
                          style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                        />
                        <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                          0 disables automated backups
                        </small>
                      </label>
                      <label>
                        <span style={{ fontSize: '13px', fontWeight: '600' }}>Backup Window (UTC)</span>
                        <input
                          value={db.backup_window || ''}
                          onChange={(e) => {
                            const newDbs = [...(boxAwsServiceConfigs.rds?.databases || [])];
                            newDbs[dbIdx] = { ...db, backup_window: e.target.value };
                            setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, rds: { ...boxAwsServiceConfigs.rds, databases: newDbs } });
                          }}
                          placeholder="03:00-04:00"
                          style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                        />
                        <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                          Format: HH:MM-HH:MM (30 min minimum)
                        </small>
                      </label>
                      <label>
                        <span style={{ fontSize: '13px', fontWeight: '600' }}>Maintenance Window (UTC)</span>
                        <input
                          value={db.maintenance_window || ''}
                          onChange={(e) => {
                            const newDbs = [...(boxAwsServiceConfigs.rds?.databases || [])];
                            newDbs[dbIdx] = { ...db, maintenance_window: e.target.value };
                            setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, rds: { ...boxAwsServiceConfigs.rds, databases: newDbs } });
                          }}
                          placeholder="sun:04:00-sun:05:00"
                          style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                        />
                        <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                          Format: ddd:HH:MM-ddd:HH:MM
                        </small>
                      </label>
                    </div>

                    {/* Tags */}
                    <h5 style={{ marginTop: '20px', marginBottom: '10px', fontSize: '14px', fontWeight: 'bold', color: darkMode ? '#60a5fa' : '#1d4ed8' }}>
                      🏷️ Tags
                    </h5>
                    <label>
                      <span style={{ fontSize: '13px', fontWeight: '600' }}>Additional Tags (comma-separated key=value)</span>
                      <input
                        value={db.tags || ''}
                        onChange={(e) => {
                          const newDbs = [...(boxAwsServiceConfigs.rds?.databases || [])];
                          newDbs[dbIdx] = { ...db, tags: e.target.value };
                          setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, rds: { ...boxAwsServiceConfigs.rds, databases: newDbs } });
                        }}
                        placeholder="Environment=Production,Owner=DBA,Purpose=AppDatabase"
                        style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                      />
                      <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                        Example: Env=prod,Owner=team,Purpose=analytics
                      </small>
                    </label>
                      </div>
                    )}
                  </div>
                );
              })}

              {/* Legacy fields - hidden but kept for backward compatibility */}
              <div style={{ display: 'none' }}>
              <label>
                Database Engine
                <select
                  value={boxAwsServiceConfigs.rds?.engine || 'mysql'}
                  onChange={(e) => {
                    setBoxAwsServiceConfigs({
                      ...boxAwsServiceConfigs,
                      rds: { ...boxAwsServiceConfigs.rds, engine: e.target.value, instance_class: '' }
                    });
                    setBoxAwsRdsData({ ...boxAwsRdsData, instance_classes: [] });
                  }}
                  disabled={boxAwsRdsDataLoading}
                >
                  {boxAwsRdsData.engines.map((engine) => (
                    <option key={engine} value={engine}>{engine}</option>
                  ))}
                </select>
              </label>
              <label>
                Instance Class
                <select
                  value={boxAwsServiceConfigs.rds?.instance_class || ''}
                  onChange={(e) => {
                    setBoxAwsServiceConfigs({
                      ...boxAwsServiceConfigs,
                      rds: { ...boxAwsServiceConfigs.rds, instance_class: e.target.value }
                    });
                  }}
                  disabled={boxAwsRdsDataLoading || !boxAwsServiceConfigs.rds?.engine}
                >
                  <option value="">Select instance class...</option>
                  {boxAwsRdsData.instance_classes.map((cls) => (
                    <option key={cls} value={cls}>{cls}</option>
                  ))}
                </select>
              </label>
              <label>
                Database Name
                <input
                  value={boxAwsServiceConfigs.rds?.db_name || ''}
                  onChange={(e) => {
                    setBoxAwsServiceConfigs({
                      ...boxAwsServiceConfigs,
                      rds: { ...boxAwsServiceConfigs.rds, db_name: e.target.value }
                    });
                  }}
                  placeholder="mydb"
                />
              </label>
              <label>
                Master Username
                <input
                  value={boxAwsServiceConfigs.rds?.username || 'admin'}
                  onChange={(e) => {
                    setBoxAwsServiceConfigs({
                      ...boxAwsServiceConfigs,
                      rds: { ...boxAwsServiceConfigs.rds, username: e.target.value }
                    });
                  }}
                />
              </label>
              <label>
                Master Password
                <input
                  type="password"
                  value={boxAwsServiceConfigs.rds?.password || ''}
                  onChange={(e) => {
                    setBoxAwsServiceConfigs({
                      ...boxAwsServiceConfigs,
                      rds: { ...boxAwsServiceConfigs.rds, password: e.target.value }
                    });
                  }}
                  placeholder="Enter password"
                />
              </label>
              {boxAwsSelectedServices.includes('vpc') && (
                <>
                  <h5>VPC & Subnet Configuration</h5>
                  <div className="info-callout" style={{ marginBottom: '10px', padding: '10px', backgroundColor: '#e6f4fa', borderRadius: '4px' }}>
                    <strong>Note:</strong> RDS will use subnets from the VPC configuration above. Select private subnets for RDS.
                  </div>
                  <label>
                    Select Subnets (at least 2 for Multi-AZ)
                    <select
                      multiple
                      value={boxAwsServiceConfigs.rds?.subnet_ids || []}
                      onChange={(e) => {
                        const selected = Array.from(e.target.selectedOptions, option => option.value);
                        setBoxAwsServiceConfigs({
                          ...boxAwsServiceConfigs,
                          rds: { ...boxAwsServiceConfigs.rds, subnet_ids: selected }
                        });
                      }}
                      style={{ width: '100%', padding: '8px', fontSize: '14px', minHeight: '100px' }}
                    >
                      {boxAwsServiceConfigs.vpc?.subnets?.map((subnet, idx) => (
                        <option key={idx} value={`module.vpc.${subnet.type}_subnet_ids[${idx}]`}>
                          {subnet.name || `Subnet ${idx + 1}`} - {subnet.cidr || 'Not configured'} ({subnet.type === 'public' ? 'Public' : 'Private'})
                        </option>
                      ))}
                    </select>
                    <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                      Hold Ctrl/Cmd to select multiple subnets. Private subnets are recommended for RDS.
                    </small>
                  </label>
                </>
              )}
              {!boxAwsSelectedServices.includes('vpc') && (
                <>
                  <h5>VPC & Subnet Configuration</h5>
                  <div className="info-callout" style={{ marginBottom: '10px', padding: '10px', backgroundColor: '#fff3cd', borderRadius: '4px' }}>
                    <strong>Note:</strong> VPC module is not selected. Please specify VPC and subnets manually below.
                  </div>
                  <label>
                    VPC ID
                    <input
                      value={boxAwsServiceConfigs.rds?.vpc_id || ''}
                      onChange={(e) => {
                        setBoxAwsServiceConfigs({
                          ...boxAwsServiceConfigs,
                          rds: { ...boxAwsServiceConfigs.rds, vpc_id: e.target.value }
                        });
                      }}
                      placeholder="vpc-xxxxxxxxx"
                    />
                  </label>
                  <label>
                    Subnet IDs (comma-separated)
                    <input
                      value={Array.isArray(boxAwsServiceConfigs.rds?.subnet_ids) ? boxAwsServiceConfigs.rds.subnet_ids.join(', ') : (boxAwsServiceConfigs.rds?.subnet_ids || '')}
                      onChange={(e) => {
                        const subnetIds = e.target.value.split(',').map(s => s.trim()).filter(s => s);
                        setBoxAwsServiceConfigs({
                          ...boxAwsServiceConfigs,
                          rds: { ...boxAwsServiceConfigs.rds, subnet_ids: subnetIds }
                        });
                      }}
                      placeholder="subnet-xxxxx, subnet-yyyyy"
                    />
                    <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                      Example subnet CIDR blocks: 10.0.1.0/24, 10.0.2.0/24 (for private subnets in different AZs)
                    </small>
                  </label>
                </>
              )}
              <label>
                Allocated Storage (GB)
                <input
                  type="number"
                  min="20"
                  value={boxAwsServiceConfigs.rds?.allocated_storage || 20}
                  onChange={(e) => {
                    setBoxAwsServiceConfigs({
                      ...boxAwsServiceConfigs,
                      rds: { ...boxAwsServiceConfigs.rds, allocated_storage: parseInt(e.target.value) || 20 }
                    });
                  }}
                />
              </label>
              <label>
                Storage Type
                <select
                  value={boxAwsServiceConfigs.rds?.storage_type || 'gp3'}
                  onChange={(e) => {
                    setBoxAwsServiceConfigs({
                      ...boxAwsServiceConfigs,
                      rds: { ...boxAwsServiceConfigs.rds, storage_type: e.target.value }
                    });
                  }}
                >
                  <option value="gp3">General Purpose SSD (gp3)</option>
                  <option value="gp2">General Purpose SSD (gp2)</option>
                  <option value="io1">Provisioned IOPS SSD (io1)</option>
                  <option value="io2">Provisioned IOPS SSD (io2)</option>
                </select>
              </label>
              <label>
                Backup Retention Period (days)
                <input
                  type="number"
                  min="0"
                  max="35"
                  value={boxAwsServiceConfigs.rds?.backup_retention_period || 7}
                  onChange={(e) => {
                    setBoxAwsServiceConfigs({
                      ...boxAwsServiceConfigs,
                      rds: { ...boxAwsServiceConfigs.rds, backup_retention_period: parseInt(e.target.value) || 7 }
                    });
                  }}
                />
              </label>
              </div>

              {/* RDS Summary */}
              <div style={{ marginTop: '25px', padding: '20px', backgroundColor: darkMode ? '#0f172a' : '#f0fdf4', borderRadius: '12px', border: darkMode ? '2px solid #3b82f6' : '2px solid #2563eb' }}>
                <h3 style={{ margin: '0 0 15px 0', fontSize: '16px', fontWeight: 'bold', color: darkMode ? '#60a5fa' : '#1d4ed8', display: 'flex', alignItems: 'center', gap: '10px' }}>
                  📊 RDS Configuration Summary
                  <span style={{ fontSize: '11px', fontWeight: 'normal', backgroundColor: '#22c55e', color: 'white', padding: '3px 8px', borderRadius: '10px' }}>
                    {boxAwsRdsCount} Database{boxAwsRdsCount > 1 ? 's' : ''}
                  </span>
                </h3>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '12px' }}>
                  {Array.from({ length: boxAwsRdsCount }).map((_, idx) => {
                    const db = boxAwsServiceConfigs.rds?.databases?.[idx] || {};
                    const engineIcon = db.engine === 'mysql' ? '🐬' : db.engine === 'postgres' ? '🐘' : db.engine === 'mariadb' ? '🦭' : '🗄️';
                    return (
                      <div key={idx} style={{ padding: '15px', backgroundColor: darkMode ? '#1e293b' : 'white', borderRadius: '10px', border: '1px solid #ddd', minWidth: '220px', flex: '1' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '10px' }}>
                          <span style={{ fontSize: '24px' }}>{engineIcon}</span>
                          <div>
                            <span style={{ backgroundColor: darkMode ? '#3b82f6' : '#2563eb', color: 'white', padding: '2px 8px', borderRadius: '4px', fontSize: '10px', fontWeight: 'bold' }}>#{idx + 1}</span>
                            <div style={{ fontSize: '14px', fontWeight: 'bold', color: darkMode ? '#e2e8f0' : '#333', marginTop: '4px' }}>{db.identifier || `database-${idx + 1}`}</div>
                          </div>
                        </div>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                          <span style={{ fontSize: '10px', padding: '3px 8px', borderRadius: '4px', backgroundColor: darkMode ? '#334155' : '#e0e7ff', color: darkMode ? '#93c5fd' : '#3730a3' }}>{db.engine || 'mysql'}</span>
                          <span style={{ fontSize: '10px', padding: '3px 8px', borderRadius: '4px', backgroundColor: darkMode ? '#334155' : '#f3f4f6' }}>{db.instance_class || 'db.t3.micro'}</span>
                          <span style={{ fontSize: '10px', padding: '3px 8px', borderRadius: '4px', backgroundColor: darkMode ? '#334155' : '#fef3c7', color: '#92400e' }}>{db.allocated_storage || 20} GB</span>
                          {db.password && <span style={{ fontSize: '10px', padding: '3px 8px', borderRadius: '4px', backgroundColor: '#dcfce7', color: '#166534' }}>✓ Password</span>}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
                </div>
              )}
            </div>
          )}

          {/* EFS Configuration */}
          {boxAwsSelectedServices.includes('efs') && (
            <div className="service-input-card">
              <div style={{ borderBottom: '1px solid #ddd', paddingBottom: '10px', marginBottom: '20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div style={{ flex: 1 }}>
                  <h2 style={{ margin: 0, fontSize: '20px', fontWeight: 'bold' }}>📂 EFS Configuration</h2>
                  <p style={{ margin: '5px 0 0 0', color: '#666', fontSize: '14px' }}>
                    Amazon EFS provides scalable, elastic file storage for Linux workloads.
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => setBoxAwsServiceExpanded({ ...boxAwsServiceExpanded, efs: !boxAwsServiceExpanded.efs })}
                  style={{
                    background: 'none',
                    border: 'none',
                    cursor: 'pointer',
                    padding: '5px 10px',
                    fontSize: '18px',
                    color: '#666',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    minWidth: '30px',
                    height: '30px'
                  }}
                  title={boxAwsServiceExpanded.efs ? 'Collapse' : 'Expand'}
                >
                  <span style={{ 
                    transform: boxAwsServiceExpanded.efs ? 'rotate(180deg)' : 'rotate(0deg)',
                    transition: 'transform 0.2s',
                    display: 'inline-block'
                  }}>
                    ▼
                  </span>
                </button>
              </div>
              {boxAwsServiceExpanded.efs && (
                <div>
                  {/* Number of EFS File Systems */}
                  <label style={{ marginBottom: '20px', display: 'block' }}>
                    <span style={{ fontWeight: 'bold', fontSize: '14px' }}>Number of EFS File Systems</span>
                    <input
                      type="number"
                      min="1"
                      max="5"
                      value={boxAwsEfsCount}
                  onChange={(e) => {
                        const count = parseInt(e.target.value) || 1;
                        setBoxAwsEfsCount(Math.min(Math.max(count, 1), 5));
                        // Initialize file systems array if needed
                        const currentFs = boxAwsServiceConfigs.efs?.filesystems || [];
                        if (count > currentFs.length) {
                          const newFs = [...currentFs];
                          for (let i = currentFs.length; i < count; i++) {
                            newFs.push({ name: `efs-${i + 1}`, performance_mode: 'generalPurpose', throughput_mode: 'bursting', storage_class: 'STANDARD', encrypted: true, enable_backup: true });
                          }
                          setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, efs: { ...boxAwsServiceConfigs.efs, filesystems: newFs } });
                        }
                      }}
                      style={{ width: '80px', padding: '8px', marginLeft: '10px' }}
                    />
                    <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                      Create up to 5 EFS file systems at once
                    </small>
              </label>

                  {/* Individual EFS Configurations */}
                  {Array.from({ length: boxAwsEfsCount }).map((_, fsIdx) => {
                    const fs = boxAwsServiceConfigs.efs?.filesystems?.[fsIdx] || { name: `efs-${fsIdx + 1}`, performance_mode: 'generalPurpose', throughput_mode: 'bursting', storage_class: 'STANDARD', encrypted: true, enable_backup: true };
                    
                    // Initialize expand state for this filesystem
                    if (boxAwsEfsFilesystemsExpanded[fsIdx] === undefined) {
                      boxAwsEfsFilesystemsExpanded[fsIdx] = true;
                    }
                    
                    return (
                      <div 
                        key={fsIdx} 
                        style={{ 
                          marginBottom: '20px', 
                          border: darkMode ? '2px solid #a78bfa' : '2px solid #8b5cf6', 
                          borderRadius: '8px',
                          overflow: 'hidden'
                        }}
                      >
                        {/* Filesystem Header */}
                        <div 
                          style={{ 
                            padding: '12px 15px', 
                            backgroundColor: darkMode ? '#5b21b6' : '#8b5cf6',
                            color: 'white',
                            display: 'flex',
                            justifyContent: 'space-between',
                            alignItems: 'center',
                            cursor: 'pointer'
                          }}
                          onClick={() => setBoxAwsEfsFilesystemsExpanded({ 
                            ...boxAwsEfsFilesystemsExpanded, 
                            [fsIdx]: !boxAwsEfsFilesystemsExpanded[fsIdx] 
                          })}
                        >
                          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                            <span style={{ 
                              backgroundColor: 'rgba(255,255,255,0.2)', 
                              padding: '4px 10px', 
                              borderRadius: '4px',
                              fontSize: '14px',
                              fontWeight: 'bold'
                            }}>
                              #{fsIdx + 1}
                            </span>
                            <span style={{ fontWeight: 'bold', fontSize: '16px' }}>
                              📂 {fs.name || `EFS ${fsIdx + 1}`}
                            </span>
                          </div>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                            {boxAwsEfsCount > 1 && (
                              <button
                                type="button"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  const newFs = [...(boxAwsServiceConfigs.efs?.filesystems || [])];
                                  newFs.splice(fsIdx, 1);
                                  setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, efs: { ...boxAwsServiceConfigs.efs, filesystems: newFs } });
                                  setBoxAwsEfsCount(Math.max(1, boxAwsEfsCount - 1));
                                }}
                                style={{
                                  background: 'rgba(255,255,255,0.2)',
                                  border: 'none',
                                  color: 'white',
                                  padding: '4px 8px',
                                  borderRadius: '4px',
                                  cursor: 'pointer',
                                  fontSize: '12px'
                                }}
                                title="Remove this filesystem"
                              >
                                🗑️ Remove
                              </button>
                            )}
                            <span style={{ 
                              transform: boxAwsEfsFilesystemsExpanded[fsIdx] ? 'rotate(180deg)' : 'rotate(0deg)',
                              transition: 'transform 0.2s',
                              display: 'inline-block'
                            }}>
                              ▼
                            </span>
                          </div>
                        </div>

                        {/* Filesystem Configuration (Expandable) */}
                        {boxAwsEfsFilesystemsExpanded[fsIdx] && (
                          <div style={{ padding: '20px', backgroundColor: darkMode ? '#1e293b' : '#faf5ff' }}>
                        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '15px' }}>
              <label>
                            <span style={{ fontSize: '13px', fontWeight: '600' }}>File System Name</span>
                <input
                              value={fs.name || ''}
                  onChange={(e) => {
                                const newFs = [...(boxAwsServiceConfigs.efs?.filesystems || [])];
                                newFs[fsIdx] = { ...fs, name: e.target.value };
                                setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, efs: { ...boxAwsServiceConfigs.efs, filesystems: newFs } });
                  }}
                              placeholder={`efs-${fsIdx + 1}`}
                              style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                />
              </label>
              <label>
                            <span style={{ fontSize: '13px', fontWeight: '600' }}>Performance Mode</span>
                <select
                              value={fs.performance_mode || 'generalPurpose'}
                  onChange={(e) => {
                                const newFs = [...(boxAwsServiceConfigs.efs?.filesystems || [])];
                                newFs[fsIdx] = { ...fs, performance_mode: e.target.value };
                                setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, efs: { ...boxAwsServiceConfigs.efs, filesystems: newFs } });
                  }}
                              style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                            >
                              <option value="generalPurpose">General Purpose</option>
                              <option value="maxIO">Max I/O</option>
                </select>
              </label>
              <label>
                            <span style={{ fontSize: '13px', fontWeight: '600' }}>Throughput Mode</span>
                            <select
                              value={fs.throughput_mode || 'bursting'}
                  onChange={(e) => {
                                const newFs = [...(boxAwsServiceConfigs.efs?.filesystems || [])];
                                newFs[fsIdx] = { ...fs, throughput_mode: e.target.value };
                                setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, efs: { ...boxAwsServiceConfigs.efs, filesystems: newFs } });
                              }}
                              style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                            >
                              <option value="bursting">Bursting</option>
                              <option value="provisioned">Provisioned</option>
                              <option value="elastic">Elastic</option>
                            </select>
              </label>
                          <label>
                            <span style={{ fontSize: '13px', fontWeight: '600' }}>Storage Class</span>
                            <select
                              value={fs.storage_class || 'STANDARD'}
                              onChange={(e) => {
                                const newFs = [...(boxAwsServiceConfigs.efs?.filesystems || [])];
                                newFs[fsIdx] = { ...fs, storage_class: e.target.value };
                                setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, efs: { ...boxAwsServiceConfigs.efs, filesystems: newFs } });
                              }}
                              style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                            >
                              <option value="STANDARD">Standard</option>
                              <option value="ONE_ZONE">One Zone</option>
                            </select>
                          </label>
                        </div>

                        {/* VPC & Network Configuration for EFS */}
                        {boxAwsSelectedServices.includes('vpc') && (
                          <>
                            <h5 style={{ marginTop: '20px', marginBottom: '10px', fontSize: '14px', fontWeight: 'bold', color: darkMode ? '#c4b5fd' : '#7c3aed' }}>
                              🌐 VPC & Network Configuration
                            </h5>
                            <div className="info-callout" style={{ marginBottom: '15px', padding: '10px', backgroundColor: darkMode ? '#1e293b' : '#e0f2fe', borderRadius: '4px', fontSize: '13px' }}>
                              <strong>Mount Targets:</strong> EFS requires mount targets in each subnet where EC2 instances will access the file system. Select at least 2 subnets for high availability.
                            </div>
                            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(250px, 1fr))', gap: '15px' }}>
                  <label>
                                <span style={{ fontSize: '13px', fontWeight: '600' }}>Select Subnets for Mount Targets</span>
                                <select
                                  multiple
                                  value={fs.subnet_ids || []}
                      onChange={(e) => {
                                    const selected = Array.from(e.target.selectedOptions, option => option.value);
                                    const newFs = [...(boxAwsServiceConfigs.efs?.filesystems || [])];
                                    newFs[fsIdx] = { ...fs, subnet_ids: selected };
                                    setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, efs: { ...boxAwsServiceConfigs.efs, filesystems: newFs } });
                                  }}
                                  style={{ width: '100%', padding: '8px', marginTop: '5px', minHeight: '100px', fontSize: '13px' }}
                                >
                                  {boxAwsServiceConfigs.vpc?.subnets?.map((subnet, idx) => (
                                    <option key={idx} value={`module.vpc.${subnet.type}_subnet_ids[${idx}]`}>
                                      {subnet.name || `Subnet ${idx + 1}`} - {subnet.cidr || 'Not configured'} ({subnet.type === 'public' ? 'Public' : 'Private'})
                                    </option>
                                  ))}
                                </select>
                                <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                                  Hold Ctrl/Cmd to select multiple. Private subnets recommended for EFS.
                                </small>
                  </label>
                  <label>
                                <span style={{ fontSize: '13px', fontWeight: '600' }}>Security Group Name</span>
                    <input
                                  value={fs.security_group_name || ''}
                      onChange={(e) => {
                                    const newFs = [...(boxAwsServiceConfigs.efs?.filesystems || [])];
                                    newFs[fsIdx] = { ...fs, security_group_name: e.target.value };
                                    setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, efs: { ...boxAwsServiceConfigs.efs, filesystems: newFs } });
                                  }}
                                  placeholder={`efs-sg-${fsIdx + 1}`}
                                  style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                                />
                                <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                                  Security group for EFS mount targets (allows NFS port 2049)
                                </small>
                  </label>
                            </div>
                </>
              )}

                        {!boxAwsSelectedServices.includes('vpc') && (
                          <div className="info-callout" style={{ marginTop: '15px', padding: '12px', backgroundColor: '#fff3cd', borderRadius: '4px', fontSize: '13px', border: '1px solid #ffc107' }}>
                            <strong>⚠️ VPC Required:</strong> EFS requires VPC configuration. Please select VPC service to configure subnets and security groups for EFS mount targets.
                </div>
              )}

                        {/* Lifecycle Policy */}
                        <h5 style={{ marginTop: '20px', marginBottom: '10px', fontSize: '14px', fontWeight: 'bold', color: darkMode ? '#c4b5fd' : '#7c3aed' }}>
                          ⏰ Lifecycle Management
                        </h5>
                        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(250px, 1fr))', gap: '15px' }}>
        <label>
                            <span style={{ fontSize: '13px', fontWeight: '600' }}>Transition to IA (Infrequent Access)</span>
                            <select
                              value={fs.transition_to_ia || 'AFTER_30_DAYS'}
                              onChange={(e) => {
                                const newFs = [...(boxAwsServiceConfigs.efs?.filesystems || [])];
                                newFs[fsIdx] = { ...fs, transition_to_ia: e.target.value };
                                setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, efs: { ...boxAwsServiceConfigs.efs, filesystems: newFs } });
                              }}
                              style={{ width: '100%', padding: '8px', marginTop: '5px' }}
                            >
                              <option value="">No lifecycle policy</option>
                              <option value="AFTER_7_DAYS">After 7 days</option>
                              <option value="AFTER_14_DAYS">After 14 days</option>
                              <option value="AFTER_30_DAYS">After 30 days</option>
                              <option value="AFTER_60_DAYS">After 60 days</option>
                              <option value="AFTER_90_DAYS">After 90 days</option>
          </select>
                            <small style={{ display: 'block', marginTop: '5px', color: '#666' }}>
                              Move files to lower-cost storage tier after inactivity
                            </small>
        </label>
                        </div>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '20px', marginTop: '15px' }}>
                          <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer' }}>
                            <input type="checkbox" checked={fs.encrypted !== false} onChange={(e) => {
                              const newFs = [...(boxAwsServiceConfigs.efs?.filesystems || [])];
                              newFs[fsIdx] = { ...fs, encrypted: e.target.checked };
                              setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, efs: { ...boxAwsServiceConfigs.efs, filesystems: newFs } });
                            }} style={{ width: '16px', height: '16px' }} />
                            <span style={{ fontSize: '13px', color: darkMode ? '#e2e8f0' : '#333' }}>🔒 Encryption</span>
          </label>
                          <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer' }}>
                            <input type="checkbox" checked={fs.enable_backup !== false} onChange={(e) => {
                              const newFs = [...(boxAwsServiceConfigs.efs?.filesystems || [])];
                              newFs[fsIdx] = { ...fs, enable_backup: e.target.checked };
                              setBoxAwsServiceConfigs({ ...boxAwsServiceConfigs, efs: { ...boxAwsServiceConfigs.efs, filesystems: newFs } });
                            }} style={{ width: '16px', height: '16px' }} />
                            <span style={{ fontSize: '13px', color: darkMode ? '#e2e8f0' : '#333' }}>📅 Backup</span>
            </label>
                        </div>
                          </div>
                        )}
            </div>
                    );
                  })}


                  {/* EFS Summary */}
                  <div style={{ padding: '20px', backgroundColor: darkMode ? '#0f172a' : '#faf5ff', borderRadius: '12px', border: darkMode ? '2px solid #a78bfa' : '2px solid #8b5cf6' }}>
                    <h3 style={{ margin: '0 0 15px 0', fontSize: '16px', fontWeight: 'bold', color: darkMode ? '#c4b5fd' : '#6d28d9', display: 'flex', alignItems: 'center', gap: '10px' }}>
                      📊 EFS Configuration Summary
                      <span style={{ fontSize: '11px', fontWeight: 'normal', backgroundColor: '#22c55e', color: 'white', padding: '3px 8px', borderRadius: '10px' }}>
                        {boxAwsEfsCount} File System{boxAwsEfsCount > 1 ? 's' : ''}
                      </span>
                    </h3>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '12px' }}>
                      {Array.from({ length: boxAwsEfsCount }).map((_, idx) => {
                        const fs = boxAwsServiceConfigs.efs?.filesystems?.[idx] || {};
                        return (
                          <div key={idx} style={{ padding: '15px', backgroundColor: darkMode ? '#1e293b' : 'white', borderRadius: '10px', border: '1px solid #ddd', minWidth: '200px', flex: '1' }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '10px' }}>
                              <span style={{ fontSize: '24px' }}>📂</span>
                              <div>
                                <span style={{ backgroundColor: darkMode ? '#a78bfa' : '#8b5cf6', color: 'white', padding: '2px 8px', borderRadius: '4px', fontSize: '10px', fontWeight: 'bold' }}>#{idx + 1}</span>
                                <div style={{ fontSize: '14px', fontWeight: 'bold', color: darkMode ? '#e2e8f0' : '#333', marginTop: '4px' }}>{fs.name || `efs-${idx + 1}`}</div>
          </div>
          </div>
                            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '5px' }}>
                              <span style={{ fontSize: '10px', padding: '3px 8px', borderRadius: '4px', backgroundColor: darkMode ? '#334155' : '#e9d5ff', color: darkMode ? '#c4b5fd' : '#7c3aed' }}>{fs.performance_mode === 'maxIO' ? 'Max I/O' : 'General'}</span>
                              <span style={{ fontSize: '10px', padding: '3px 8px', borderRadius: '4px', backgroundColor: darkMode ? '#334155' : '#f3f4f6', textTransform: 'capitalize' }}>{fs.throughput_mode || 'bursting'}</span>
                              {fs.encrypted !== false && <span style={{ fontSize: '10px', padding: '3px 8px', borderRadius: '4px', backgroundColor: '#dcfce7', color: '#166534' }}>🔒</span>}
                              {fs.enable_backup !== false && <span style={{ fontSize: '10px', padding: '3px 8px', borderRadius: '4px', backgroundColor: '#dbeafe', color: '#1e40af' }}>📅</span>}
        </div>
          </div>
                        );
                      })}
            </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </fieldset>
      )}
      <fieldset>
        <legend>Generate Terraform</legend>
        <button 
          onClick={runBoxProjectAwsTask} 
          disabled={!boxAwsSelectedServices.length || !boxAwsSelectedRegion.trim()}
        >
          Build AWS Terraform Project
        </button>
        {boxAwsError && <div className="error">{boxAwsError}</div>}
        <h3>Logs</h3>
        <pre>{boxAwsLogs || 'Logs will appear here once a run starts.'}</pre>
        <h3>Artifacts</h3>
        <div className="artifacts">
          {boxAwsArtifacts.map((artifact) => (
            <a className="download-link" key={artifact.url} href={artifact.url} download={artifact.filename}>
              Download {artifact.filename}
            </a>
          ))}
        </div>
      </fieldset>
      </>
      )}

      

      {isVpnView && (
      <>
      <fieldset className={showGcpSubnetOverlay ? 'fieldset-overlay' : undefined}>
        <legend>{vpnLegendLabel} - GCP Credentials & Network</legend>
        {showGcpSubnetOverlay && (
          <div className="loading-overlay">
            <div>
              <strong>Loading GCP subnets...</strong>
              <small>Please wait until loading completes before changing region or network.</small>
            </div>
          </div>
        )}
        <label>
          Upload Service Account JSON
          <input type="file" accept="application/json,.json" onChange={(e) => handleServiceKeyFile(e.target.files?.[0])} />
        </label>
          {vpnServiceFileName && (
            <small className="file-indicator">
              Loaded: {vpnServiceFileName}
              <button type="button" onClick={clearServiceKey}>
                Clear
              </button>
            </small>
          )}
          {vpnProjectOptions.length > 0 ? (
            <label>
              GCP Project
              <select value={vpnGcpProject} onChange={(e) => setVpnGcpProject(e.target.value)}>
                {vpnProjectOptions.map((project) => (
                  <option key={project.project_id || project.name} value={project.project_id}>
                    {project.display_name && project.display_name !== project.project_id
                      ? `${project.display_name} (${project.project_id})`
                      : project.project_id}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <div className="info-callout">No GCP projects detected for this service account.</div>
          )}
          {vpnProjectError && <div className="error">{vpnProjectError}</div>}
        <label>
          GCP Region
          <select value={vpnGcpRegion} onChange={(e) => setVpnGcpRegion(e.target.value)} disabled={vpnSubnetsLoading}>
            {GCP_REGIONS.map((region) => (
              <option key={region.id} value={region.id}>
                {region.display}
              </option>
            ))}
          </select>
        </label>
        <label>
          GCP VPC Network
          <select value={vpnGcpNetwork} onChange={(e) => setVpnGcpNetwork(e.target.value)} disabled={vpnSubnetsLoading}>
            <option value="">-- Select network --</option>
            {vpnGcpNetworks.map((network) => (
              <option key={network.name} value={network.name}>
                {network.name} {network.auto_create_subnetworks ? '(auto)' : ''}
              </option>
              ))}
            </select>
          </label>
          {vpnNetworkError && <div className="error">{vpnNetworkError}</div>}
          {showGcpSubnetOverlay && !vpnSubnetError && (
            <small>Loading subnets for {vpnGcpNetwork || 'selected network'}...</small>
          )}
          {vpnSubnetError && <div className="error">{vpnSubnetError}</div>}
          {showGcpSubnetSelector && (
            <div className="aws-subnet-selector">
              <div className="label-row">
                <label>GCP Subnets</label>
                <div className="pill-actions">
                  <button type="button" onClick={selectAllGcpSubnets} disabled={!gcpSubnetOptions.length}>
                    Select all
                  </button>
                  <button type="button" onClick={clearGcpSubnets} disabled={!selectedGcpSubnets.length}>
                    Clear
                  </button>
                </div>
              </div>
              <small>Choose which GCP subnetworks to include; selected CIDRs will be advertised on the Cloud Router.</small>
              <div className="checkbox-grid">
                {gcpSubnetOptions.map((subnet) => (
                  <label key={subnet.name} className="checkbox-item">
                    <input
                      type="checkbox"
                      checked={selectedGcpSubnets.includes(subnet.name)}
                      onChange={() => toggleGcpSubnetSelection(subnet.name)}
                    />
                    <span>
                      {subnet.name} ({subnet.ipCidrRange || subnet.cidr || subnet.ip_cidr_range || '?'}) @ {subnet.region}
                    </span>
                  </label>
                ))}
              </div>
            </div>
          )}
        </fieldset>
          {view === 'ha_vpn' && (
            <fieldset>
              <legend>HA VPN Plan</legend>
              <label>
                AWS ASN
                <input
                  type="number"
                  min="1"
                  value={haAwsAsn}
                  onChange={(e) => setHaAwsAsn(e.target.value)}
                  onBlur={handleHaAwsAsnBlur}
                  disabled={Boolean(detectedAwsAsn)}
                  title={detectedAwsAsn ? `Detected attached VGW ASN ${detectedAwsAsn}` : undefined}
                  className={haAsnError ? 'error-input' : undefined}
                />
              </label>
              {attachedVgw && (
                <small className="info-callout">
                  Using existing VGW {attachedVgw.name ? `${attachedVgw.name} (${attachedVgw.id})` : attachedVgw.id} with ASN {attachedVgw.asn || 'unknown'}. Only one VGW can be attached to a VPC at a time, so we will reuse this gateway.
                </small>
              )}
              <label>
                GCP ASN
                <input
                  type="number"
                  min={minAsn}
                  max={maxAsn}
                  value={haGcpAsn}
                  onChange={(e) => setHaGcpAsn(e.target.value)}
                  onBlur={handleHaGcpAsnBlur}
                  className={haAsnError ? 'error-input' : undefined}
                  title="ASN range 64512-65534; must differ from AWS ASN."
                />
              </label>
              {haAsnError && <div className="error">{haAsnError}</div>}
              <label>
                Resource Name Prefix (optional)
                <input value={haPrefix} onChange={(e) => setHaPrefix(e.target.value)} placeholder="ha-shared-vpn" />
              </label>
              <div className="info-callout">
                This action provisions AWS and GCP HA VPN resources (VGWs, gateways, tunnels, and BGP sessions). Ensure the supplied credentials have the required permissions.
              </div>
              <button onClick={runHaVpnTask} disabled={!selectedVpc || !authReady}>
                Provision HA VPN
              </button>
              {haError && <div className="error">{haError}</div>}
              <h3>Logs</h3>
              <pre>{haLogs}</pre>
              <h3>Artifacts</h3>
              <div className="artifacts">
                {haArtifacts.map((artifact) => (
                  <a className="download-link" key={artifact.url} href={artifact.url} download={artifact.filename}>
                    Download {artifact.filename}
                  </a>
                ))}
              </div>
            </fieldset>
          )}
          {view === 'classic_vpn' && (
            <fieldset>
              <legend>Classic VPN Plan</legend>
              <label>
                AWS ASN
                <input
                  type="number"
                  min="1"
                  value={classicAwsAsn}
                  onChange={(e) => setClassicAwsAsn(e.target.value)}
                  onBlur={handleClassicAwsAsnBlur}
                  disabled={Boolean(detectedAwsAsn)}
                  title={detectedAwsAsn ? `Detected attached VGW ASN ${detectedAwsAsn}` : undefined}
                  className={classicAsnError ? 'error-input' : undefined}
                />
              </label>
              {attachedVgw && (
                <small className="info-callout">
                  Using existing VGW {attachedVgw.name ? `${attachedVgw.name} (${attachedVgw.id})` : attachedVgw.id} with ASN {attachedVgw.asn || 'unknown'}. Only one VGW can be attached to a VPC at a time, so we will reuse this gateway.
                </small>
              )}
              <label>
                GCP ASN
                <input
                  type="number"
                  min={minAsn}
                  max={maxAsn}
                  value={classicGcpAsn}
                  onChange={(e) => setClassicGcpAsn(e.target.value)}
                  onBlur={handleClassicGcpAsnBlur}
                  className={classicAsnError ? 'error-input' : undefined}
                  title="ASN range 64512-65534; must differ from AWS ASN."
                />
              </label>
              {classicAsnError && <div className="error">{classicAsnError}</div>}
              <label>
                Resource Name Prefix (optional)
                <input value={classicPrefix} onChange={(e) => setClassicPrefix(e.target.value)} placeholder="classic-shared-vpn" />
              </label>
              <label>
                IKE Version
                <select value={classicIkeVersion} onChange={(e) => setClassicIkeVersion(e.target.value)}>
                  <option value="1">IKEv1</option>
                  <option value="2">IKEv2</option>
                </select>
              </label>
              <div className="info-callout">
                This action provisions AWS and GCP Classic VPN resources (VGW, tunnels, and BGP). Ensure the supplied credentials have the required permissions.
              </div>
              <button onClick={runClassicVpnTask} disabled={!selectedVpc || !authReady || vpnSubnetsLoading}>
                Provision Classic VPN
              </button>
              {classicError && <div className="error">{classicError}</div>}
              <h3>Logs</h3>
              <pre>{classicLogs}</pre>
              <h3>Artifacts</h3>
              <div className="artifacts">
                {classicArtifacts.map((artifact) => (
                  <a className="download-link" key={artifact.url} href={artifact.url} download={artifact.filename}>
                    Download {artifact.filename}
                  </a>
                ))}
              </div>
            </fieldset>
          )}
      </>
      )}

      {view === 'ecr_migration' && (
      <motion.fieldset
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
      >
        <legend>ECR to Artifact Registry</legend>
        <div className="aws-subnet-selector">
          <div className="label-row">
            <label>ECR Repositories</label>
            <div className="pill-actions">
              <button type="button" onClick={() => setSelectedEcrRepos(ecrRepos.map((r) => r.name))} disabled={!ecrRepos.length}>
                Select all
              </button>
              <button type="button" onClick={() => setSelectedEcrRepos([])} disabled={!selectedEcrRepos.length}>
                Clear
              </button>
            </div>
          </div>
          {ecrRepoError && <div className="error">{ecrRepoError}</div>}
          <div className="checkbox-grid">
            {ecrRepos.map((repo) => (
              <label key={repo.name} className="checkbox-item">
                <input
                  type="checkbox"
                  checked={selectedEcrRepos.includes(repo.name)}
                  onChange={() =>
                    setSelectedEcrRepos((prev) =>
                      prev.includes(repo.name) ? prev.filter((n) => n !== repo.name) : [...prev, repo.name]
                    )
                  }
                />
                <span>{repo.name} ({repo.image_count ?? 0} images)</span>
              </label>
            ))}
          </div>
        </div>
        <label>
          Upload GCP Service Account JSON
          <input
            type="file"
            accept="application/json,.json"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (!file) {
                setEcrServiceKey('');
                setEcrServiceFileName('');
                setEcrProjectOptions([]);
                setEcrProjectError('');
                return;
              }
              
              // Check file size (max 1MB)
              if (file.size > 1024 * 1024) {
                setEcrProjectError('File is too large. Maximum size is 1MB.');
                setEcrServiceKey('');
                setEcrServiceFileName('');
                return;
              }
              
              // Check if file is empty
              if (file.size === 0) {
                setEcrProjectError('File is empty. Please select a valid JSON file.');
                setEcrServiceKey('');
                setEcrServiceFileName('');
                return;
              }
              
              setEcrServiceFileName(file.name);
              setEcrProjectError('');
              
              const reader = new FileReader();
              reader.onerror = () => {
                setEcrProjectError('Failed to read file. Please try again.');
                setEcrServiceKey('');
                setEcrServiceFileName('');
              };
              reader.onload = (evt) => {
                const content = evt.target?.result?.toString() || '';
                if (!content.trim()) {
                  setEcrProjectError('File appears to be empty. Please select a valid JSON file.');
                  setEcrServiceKey('');
                  setEcrServiceFileName('');
                  return;
                }
                
                // Validate JSON before setting
                try {
                  JSON.parse(content);
                  setEcrServiceKey(content);
                  setEcrProjectError('');
                } catch (err) {
                  setEcrProjectError(`Invalid JSON: ${err.message}`);
                  setEcrServiceKey('');
                  setEcrServiceFileName('');
                }
              };
              reader.readAsText(file);
            }}
          />
          {ecrServiceFileName && <small className="file-indicator">Loaded: {ecrServiceFileName}</small>}
        </label>
        {ecrProjectOptions.length > 0 ? (
          <label>
            GCP Project
            <select value={ecrGcpProject} onChange={(e) => setEcrGcpProject(e.target.value)}>
              {ecrProjectOptions.map((project) => (
                <option key={project.project_id || project.name} value={project.project_id}>
                  {project.display_name && project.display_name !== project.project_id
                    ? `${project.display_name} (${project.project_id})`
                    : project.project_id}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <label>
            GCP Project ID
            <input value={ecrGcpProject} onChange={(e) => setEcrGcpProject(e.target.value)} placeholder="my-gcp-project" />
          </label>
        )}
        {ecrProjectError && <div className="error">{ecrProjectError}</div>}
        <label>
          Artifact Registry Region
          <select value={ecrGcpRegion} onChange={(e) => setEcrGcpRegion(e.target.value)}>
            {GCP_REGIONS.map((region) => (
              <option key={region.id} value={region.id}>
                {region.display}
              </option>
            ))}
          </select>
        </label>
        <label>
          Parallel Workers (repos & images)
          <input
            type="number"
            min="1"
            value={ecrWorkers}
            onChange={(e) => setEcrWorkers(e.target.value)}
            placeholder="4"
          />
          <small>Max recommended: {ecrMaxWorkers}</small>
        </label>
        <div className="info-callout">
          This action pulls images from AWS ECR and pushes them into GCP Artifact Registry using Docker and gcloud.
          Ensure both CLIs are installed on the backend host and the service account has Artifact Registry permissions.
        </div>
        <button onClick={runEcrMigration} disabled={!authReady}>
          Run Migration
        </button>
        {ecrError && <div className="error">{ecrError}</div>}
        <h3>Logs</h3>
        <pre>{ecrLogs || 'Logs will appear here once a run starts.'}</pre>
        <h3>Artifacts</h3>
        <div className="artifacts">
          {ecrArtifacts.map((artifact) => (
            <a className="download-link" key={artifact.url} href={artifact.url} download={artifact.filename}>
              Download {artifact.filename}
            </a>
          ))}
        </div>
      </motion.fieldset>
      )}

      {view === 'security_audit' && (
      <motion.fieldset
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
      >
        <legend>Standard Security Audit</legend>
        <div className="info-callout">
          Generates a multi-tab XLSX report with the same formatting and colors as the original sheet template.
        </div>
        <button onClick={runSecurityAudit} disabled={!auditReady || auditLoading}>
          {auditLoading ? 'Running...' : 'Run Security Audit'}
        </button>
        {auditError && <div className="error">{auditError}</div>}
        <h3>Audit Logs</h3>
        <pre>{auditLogs || 'Logs will appear here once a run starts.'}</pre>
        <h3>Audit Artifacts</h3>
        <div className="artifacts">
          {auditArtifacts.map((artifact) => (
            <a className="download-link" key={artifact.url} href={artifact.url} download={artifact.filename}>
              Download {artifact.filename}
            </a>
          ))}
        </div>
      </motion.fieldset>
      )}

      {view === 'gcp_security_audit' && (
      <motion.fieldset
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
      >
        <legend>GCP Security Audit</legend>
        <label>
          Upload GCP Service Account JSON
          <input
            type="file"
            accept="application/json,.json"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (!file) {
                setGcpAuditServiceKey('');
                setGcpAuditServiceFileName('');
                setGcpAuditProjectError('');
                return;
              }
              
              // Check file size (max 1MB)
              if (file.size > 1024 * 1024) {
                setGcpAuditProjectError('File is too large. Maximum size is 1MB.');
                setGcpAuditServiceKey('');
                setGcpAuditServiceFileName('');
                return;
              }
              
              // Check if file is empty
              if (file.size === 0) {
                setGcpAuditProjectError('File is empty. Please select a valid JSON file.');
                setGcpAuditServiceKey('');
                setGcpAuditServiceFileName('');
                return;
              }
              
              setGcpAuditServiceFileName(file.name);
              setGcpAuditProjectError('');
              
              const reader = new FileReader();
              reader.onerror = () => {
                setGcpAuditProjectError('Failed to read file. Please try again.');
                setGcpAuditServiceKey('');
                setGcpAuditServiceFileName('');
              };
              reader.onload = (evt) => {
                const content = evt.target?.result?.toString() || '';
                if (!content.trim()) {
                  setGcpAuditProjectError('File appears to be empty. Please select a valid JSON file.');
                  setGcpAuditServiceKey('');
                  setGcpAuditServiceFileName('');
                  return;
                }
                
                // Validate JSON before setting
                try {
                  JSON.parse(content);
                  setGcpAuditServiceKey(content);
                  setGcpAuditProjectError('');
                } catch (err) {
                  setGcpAuditProjectError(`Invalid JSON: ${err.message}`);
                  setGcpAuditServiceKey('');
                  setGcpAuditServiceFileName('');
                }
              };
              reader.readAsText(file);
            }}
          />
          {gcpAuditServiceFileName && <small className="file-indicator">Loaded: {gcpAuditServiceFileName}</small>}
        </label>
        <div className="aws-subnet-selector">
          <div className="label-row">
            <label>Projects</label>
            <div className="pill-actions">
              <button type="button" onClick={selectAllGcpAuditProjects} disabled={!gcpAuditProjects.length}>
                Select all
              </button>
              <button type="button" onClick={clearGcpAuditProjects} disabled={!selectedGcpAuditProjects.length}>
                Clear
              </button>
            </div>
          </div>
          {gcpAuditProjectsLoading && <small>Loading projects...</small>}
          {gcpAuditProjectError && <div className="error">{gcpAuditProjectError}</div>}
          {!gcpAuditProjectsLoading && !gcpAuditProjects.length && !gcpAuditProjectError && (
            <div className="info-callout">No GCP projects detected for this service account.</div>
          )}
          <div className="checkbox-grid">
            {gcpAuditProjects.map((project) => (
              <label key={project.project_id} className="checkbox-item">
                <input
                  type="checkbox"
                  checked={selectedGcpAuditProjects.includes(project.project_id)}
                  onChange={() => toggleGcpAuditProject(project.project_id)}
                />
                <span>
                  {project.display_name && project.display_name !== project.project_id
                    ? `${project.display_name} (${project.project_id})`
                    : project.project_id}
                </span>
              </label>
            ))}
          </div>
        </div>
        <div className="info-callout">
          Generates a multi-tab XLSX report for the selected projects.
        </div>
        <button
          onClick={runGcpSecurityAudit}
          disabled={!gcpAuditServiceKey.trim() || gcpAuditLoading || !selectedGcpAuditProjects.length}
        >
          {gcpAuditLoading ? 'Running...' : 'Run GCP Security Audit'}
        </button>
        {gcpAuditError && <div className="error">{gcpAuditError}</div>}
        <h3>Audit Logs</h3>
        <pre>{gcpAuditLogs || 'Logs will appear here once a run starts.'}</pre>
        <h3>Audit Artifacts</h3>
        <div className="artifacts">
          {gcpAuditArtifacts.map((artifact) => (
            <a className="download-link" key={artifact.url} href={artifact.url} download={artifact.filename}>
              Download {artifact.filename}
            </a>
          ))}
        </div>
      </motion.fieldset>
      )}

      {view === 'tco_report' && (
      <motion.fieldset
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
      >
        <legend>AWS TCO Report</legend>
        <label>
          Upload AWS Billing CSV
          <input
            type="file"
            accept=".csv,text/csv"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (!file) {
                setTcoCsvContent('');
                setTcoFileName('');
                return;
              }
              setTcoFileName(file.name);
              const reader = new FileReader();
              reader.onload = (evt) => {
                setTcoCsvContent(evt.target?.result?.toString() || '');
              };
              reader.readAsText(file);
            }}
          />
          {tcoFileName && <small className="file-indicator">Loaded: {tcoFileName}</small>}
        </label>
        <div className="info-callout">
          The report includes service summaries and region-level compute breakdowns based on the uploaded CSV.
        </div>
        <button onClick={runTcoReport} disabled={!tcoCsvContent.trim() || tcoLoading}>
          {tcoLoading ? 'Running...' : 'Run TCO Report'}
        </button>
        {tcoError && <div className="error">{tcoError}</div>}
        <h3>Report Logs</h3>
        <pre>{tcoLogs || 'Logs will appear here once a run starts.'}</pre>
        <h3>Report Artifacts</h3>
        <div className="artifacts">
          {tcoArtifacts.map((artifact) => (
            <a className="download-link" key={artifact.url} href={artifact.url} download={artifact.filename}>
              Download {artifact.filename}
            </a>
          ))}
        </div>
      </motion.fieldset>
      )}

      {view === 'inventory' && (
      <motion.fieldset
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
      >
        <legend>Quick AWS Inventory</legend>
        <div className="flex-row">
          <div>
            <div className="label-row">
              <label>Regions</label>
              <div className="pill-actions">
                <button type="button" onClick={setAllRegions} disabled={allRegionsSelected}>
                  Select all
                </button>
                <button type="button" onClick={clearRegions} disabled={!invRegions.length}>
                  Clear
                </button>
              </div>
            </div>
            <div className="checkbox-grid">
              {AWS_REGION_CHOICES.map((region) => (
                <label key={region.id} className="checkbox-item">
                  <input
                    type="checkbox"
                    checked={invRegions.includes(region.id)}
                    onChange={() => toggleInventoryRegion(region.id)}
                  />
                  <span>{getAwsRegionDisplay(region)}</span>
                </label>
              ))}
            </div>
          </div>
          <div>
            <div className="label-row">
              <label>Resources</label>
              <div className="pill-actions">
                <button type="button" onClick={selectAllResources} disabled={allResourcesSelected}>
                  Select all
                </button>
                <button type="button" onClick={clearResources} disabled={!invResources.length}>
                  Clear
                </button>
              </div>
            </div>
            <div className="checkbox-grid">
              {INVENTORY_RESOURCE_CHOICES.map((resource) => (
                <label key={resource.id} className="checkbox-item">
                  <input
                    type="checkbox"
                    checked={invResources.includes(resource.id)}
                    onChange={() => toggleInventoryResource(resource.id)}
                  />
                  <span>{resource.label}</span>
                </label>
              ))}
            </div>
          </div>
        </div>
        <div className="flex-row">
          <div>
            <label>
              From Date
              <input type="date" value={invFrom} onChange={(e) => setInvFrom(e.target.value)} />
            </label>
          </div>
          <div>
            <label>
              To Date
              <input type="date" value={invTo} onChange={(e) => setInvTo(e.target.value)} />
            </label>
          </div>
        </div>
        <button onClick={runInventory} disabled={!authReady || invLoading}>
          {invLoading ? 'Running...' : 'Run AWS Inventory'}
        </button>
        {invLoading && (
          <div className="progress-indicator" role="status">
            <div className="progress-bar" />
            <span>Running inventory...</span>
          </div>
        )}
        {invStatus && <small className="status-line">{invStatus}</small>}
        {invError && <div className="error">{invError}</div>}
        <h3>Inventory Logs</h3>
        <pre>{currentInvLogText}</pre>
        <h3>Inventory Artifacts</h3>
        <div className="artifacts">
          {invArtifacts.map((artifact) => (
            <a className="download-link" key={artifact.url} href={artifact.url} download={artifact.filename}>
              Download {artifact.filename}
            </a>
          ))}
        </div>
      </motion.fieldset>
      )}

      {/* SSH Key Pair Modal */}
      {boxAwsGeneratedKeyPair && (
        <div style={{
          position: 'fixed',
          top: 0,
          left: 0,
          right: 0,
          bottom: 0,
          backgroundColor: 'rgba(0, 0, 0, 0.7)',
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'center',
          zIndex: 10000
        }}>
          <div style={{
            backgroundColor: darkMode ? '#1e293b' : '#ffffff',
            borderRadius: '12px',
            padding: '24px',
            width: '90%',
            maxWidth: '700px',
            maxHeight: '90vh',
            overflow: 'auto',
            boxShadow: '0 25px 50px -12px rgba(0, 0, 0, 0.5)'
          }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px' }}>
              <h2 style={{ margin: 0, color: darkMode ? '#60a5fa' : '#0073bb' }}>
                🔑 Key Pair Generated: {boxAwsGeneratedKeyPair.key_name}
              </h2>
              <button
                onClick={() => setBoxAwsGeneratedKeyPair(null)}
                style={{
                  background: 'none',
                  border: 'none',
                  fontSize: '24px',
                  cursor: 'pointer',
                  color: darkMode ? '#94a3b8' : '#666'
                }}
              >
                ×
              </button>
            </div>

            <div style={{
              backgroundColor: darkMode ? '#0f172a' : '#fef3c7',
              border: darkMode ? '1px solid #f59e0b' : '1px solid #f59e0b',
              borderRadius: '8px',
              padding: '12px',
              marginBottom: '20px',
              color: darkMode ? '#fbbf24' : '#92400e'
            }}>
              <strong>⚠️ Important:</strong> Copy and save your private key now. It will NOT be stored and cannot be retrieved later!
            </div>

            <div style={{ marginBottom: '20px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                <label style={{ fontWeight: 'bold', color: darkMode ? '#e2e8f0' : '#333' }}>Private Key (.pem)</label>
                <div style={{ display: 'flex', gap: '8px' }}>
                  <button
                    onClick={() => {
                      navigator.clipboard.writeText(boxAwsGeneratedKeyPair.private_key);
                      alert('Private key copied to clipboard!');
                    }}
                    style={{
                      padding: '6px 12px',
                      backgroundColor: darkMode ? '#334155' : '#e2e8f0',
                      border: 'none',
                      borderRadius: '4px',
                      cursor: 'pointer',
                      fontSize: '13px',
                      color: darkMode ? '#e2e8f0' : '#333'
                    }}
                  >
                    📋 Copy
                  </button>
                  <button
                    onClick={() => {
                      const blob = new Blob([boxAwsGeneratedKeyPair.private_key], { type: 'text/plain' });
                      const url = URL.createObjectURL(blob);
                      const a = document.createElement('a');
                      a.href = url;
                      a.download = `${boxAwsGeneratedKeyPair.key_name}.pem`;
                      document.body.appendChild(a);
                      a.click();
                      document.body.removeChild(a);
                      URL.revokeObjectURL(url);
                    }}
                    style={{
                      padding: '6px 12px',
                      backgroundColor: '#0073bb',
                      color: 'white',
                      border: 'none',
                      borderRadius: '4px',
                      cursor: 'pointer',
                      fontSize: '13px'
                    }}
                  >
                    💾 Download .pem
                  </button>
                </div>
              </div>
              <textarea
                readOnly
                value={boxAwsGeneratedKeyPair.private_key}
                style={{
                  width: '100%',
                  height: '200px',
                  fontFamily: 'monospace',
                  fontSize: '12px',
                  padding: '12px',
                  borderRadius: '6px',
                  border: darkMode ? '1px solid #475569' : '1px solid #d1d5db',
                  backgroundColor: darkMode ? '#0f172a' : '#f8fafc',
                  color: darkMode ? '#22c55e' : '#166534',
                  resize: 'vertical'
                }}
              />
            </div>

            <div style={{ marginBottom: '20px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                <label style={{ fontWeight: 'bold', color: darkMode ? '#e2e8f0' : '#333' }}>Public Key</label>
                <button
                  onClick={() => {
                    navigator.clipboard.writeText(boxAwsGeneratedKeyPair.public_key);
                    alert('Public key copied to clipboard!');
                  }}
                  style={{
                    padding: '6px 12px',
                    backgroundColor: darkMode ? '#334155' : '#e2e8f0',
                    border: 'none',
                    borderRadius: '4px',
                    cursor: 'pointer',
                    fontSize: '13px',
                    color: darkMode ? '#e2e8f0' : '#333'
                  }}
                >
                  📋 Copy
                </button>
              </div>
              <textarea
                readOnly
                value={boxAwsGeneratedKeyPair.public_key}
                style={{
                  width: '100%',
                  height: '80px',
                  fontFamily: 'monospace',
                  fontSize: '12px',
                  padding: '12px',
                  borderRadius: '6px',
                  border: darkMode ? '1px solid #475569' : '1px solid #d1d5db',
                  backgroundColor: darkMode ? '#0f172a' : '#f8fafc',
                  color: darkMode ? '#60a5fa' : '#1d4ed8',
                  resize: 'vertical'
                }}
              />
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
              <button
                onClick={() => setBoxAwsGeneratedKeyPair(null)}
                style={{
                  padding: '10px 24px',
                  backgroundColor: '#10b981',
                  color: 'white',
                  border: 'none',
                  borderRadius: '6px',
                  cursor: 'pointer',
                  fontWeight: 'bold',
                  fontSize: '14px'
                }}
              >
                ✓ Done
              </button>
            </div>
          </div>
        </div>
      )}

      {/* DocumentDB to Atlas Migration */}
      {view === 'docdb_migration' && (
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5 }}
          style={{ marginTop: '20px' }}
        >
          <fieldset>
            <legend>DocumentDB to Atlas Migration</legend>
            
            <div style={{ 
              marginBottom: '25px',
              padding: '20px',
              backgroundColor: darkMode ? '#1e293b' : '#eff6ff',
              border: darkMode ? '1px solid #3b82f6' : '1px solid #93c5fd',
              borderRadius: '8px',
              borderLeft: darkMode ? '4px solid #3b82f6' : '4px solid #2563eb'
            }}>
              <div style={{ 
                display: 'flex', 
                alignItems: 'center', 
                marginBottom: '12px',
                gap: '10px'
              }}>
                <span style={{ fontSize: '24px' }}>🔄</span>
                <h3 style={{ 
                  margin: 0, 
                  fontSize: '18px',
                  fontWeight: 'bold',
                  color: darkMode ? '#60a5fa' : '#1e40af'
                }}>
                  What does this do?
                </h3>
              </div>
              <p style={{ 
                margin: '0 0 12px 0',
                color: darkMode ? '#cbd5e1' : '#1f2937',
                lineHeight: '1.6'
              }}>
                This tool migrates data from AWS DocumentDB to MongoDB Atlas with support for:
              </p>
              <ul style={{ 
                marginLeft: '20px', 
                marginBottom: 0,
                color: darkMode ? '#cbd5e1' : '#374151',
                lineHeight: '1.8'
              }}>
                <li><strong style={{ color: darkMode ? '#e2e8f0' : '#111827' }}>Fresh Migration:</strong> Complete data replacement (drops existing collections)</li>
                <li><strong style={{ color: darkMode ? '#e2e8f0' : '#111827' }}>Incremental Migration:</strong> Non-destructive sync of changes since last run</li>
                <li><strong style={{ color: darkMode ? '#e2e8f0' : '#111827' }}>Index Reconciliation:</strong> Automatically recreates indexes to match source</li>
              </ul>
            </div>

            <div style={{
              marginBottom: '20px',
              paddingBottom: '20px',
              borderBottom: darkMode ? '2px solid #334155' : '2px solid #e5e7eb'
            }}>
              <h3 style={{
                fontSize: '16px',
                fontWeight: 'bold',
                marginBottom: '15px',
                color: darkMode ? '#e2e8f0' : '#111827',
                display: 'flex',
                alignItems: 'center',
                gap: '8px'
              }}>
                <span>⚡</span> Migration Action
              </h3>
              <label>
                Action
                <select value={docdbAction} onChange={(e) => setDocdbAction(e.target.value)}>
                  <option value="migrate">Run Migration</option>
                  <option value="init_last_run">Initialize Last Run Timestamp</option>
                  <option value="estimate">Estimate Migration Size & Time</option>
                </select>
              </label>
            </div>

            <div style={{
              marginBottom: '20px',
              paddingBottom: '20px',
              borderBottom: darkMode ? '2px solid #334155' : '2px solid #e5e7eb'
            }}>
              <h3 style={{
                fontSize: '16px',
                fontWeight: 'bold',
                marginBottom: '15px',
                color: darkMode ? '#e2e8f0' : '#111827',
                display: 'flex',
                alignItems: 'center',
                gap: '8px'
              }}>
                <span>🔌</span> Connection Strings
              </h3>
              <label>
                Atlas Connection String
              <input
                type="text"
                value={docdbAtlasUri}
                onChange={(e) => setDocdbAtlasUri(e.target.value)}
                placeholder="mongodb+srv://username:password@cluster.mongodb.net/"
              />
              <small style={{ display: 'block', marginTop: '5px', color: darkMode ? '#94a3b8' : '#6b7280' }}>
                Your MongoDB Atlas connection string
              </small>
            </label>

            <label>
              DocumentDB Connection String
              <input
                type="text"
                value={docdbDocdbUri}
                onChange={(e) => setDocdbDocdbUri(e.target.value)}
                placeholder="mongodb://username:password@docdb-cluster.region.docdb.amazonaws.com:27017/"
              />
              <small style={{ display: 'block', marginTop: '5px', color: darkMode ? '#94a3b8' : '#6b7280' }}>
                Your DocumentDB (read-only) connection string
              </small>
            </label>
            </div>

            {docdbAction === 'migrate' && (
              <>
                <div style={{
                  marginBottom: '20px',
                  paddingBottom: '20px',
                  borderBottom: darkMode ? '2px solid #334155' : '2px solid #e5e7eb'
                }}>
                  <h3 style={{
                    fontSize: '16px',
                    fontWeight: 'bold',
                    marginBottom: '15px',
                    color: darkMode ? '#e2e8f0' : '#111827',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px'
                  }}>
                    <span>⚙️</span> Migration Configuration
                  </h3>
                  <label>
                    Migration Mode
                  <select value={docdbMode} onChange={(e) => setDocdbMode(e.target.value)}>
                    <option value="fresh">Fresh Migration (drop & reload)</option>
                    <option value="incremental">Incremental Migration (non-destructive)</option>
                  </select>
                  <small style={{ display: 'block', marginTop: '5px', color: darkMode ? '#94a3b8' : '#6b7280' }}>
                    {docdbMode === 'fresh' 
                      ? '⚠️ This will drop existing collections in Atlas before migrating'
                      : '✅ This will only insert/update changed documents since last run'
                    }
                  </small>
                </label>

                <label>
                  Databases to Migrate
                  <textarea
                    value={docdbDatabases}
                    onChange={(e) => setDocdbDatabases(e.target.value)}
                    placeholder="Leave empty to migrate all databases, or enter one database name per line:"
                    rows={4}
                    style={{
                      fontFamily: 'monospace',
                      fontSize: '13px'
                    }}
                  />
                  <small style={{ display: 'block', marginTop: '5px', color: darkMode ? '#94a3b8' : '#6b7280' }}>
                    Leave empty to migrate all databases (except admin, local, config)
                  </small>
                </label>

                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '15px' }}>
                  <label>
                    Insertion Workers per Collection
                    <input
                      type="number"
                      min="1"
                      max="32"
                      value={docdbNumWorkers}
                      onChange={(e) => setDocdbNumWorkers(parseInt(e.target.value) || 8)}
                    />
                  </label>

                  <label>
                    Parallel Collections
                    <input
                      type="number"
                      min="1"
                      max="16"
                      value={docdbNumParallelCollections}
                      onChange={(e) => setDocdbNumParallelCollections(parseInt(e.target.value) || 4)}
                    />
                  </label>
                </div>

                {docdbMode === 'incremental' && (
                  <label>
                    Timestamp Field
                    <input
                      type="text"
                      value={docdbTimestampField}
                      onChange={(e) => setDocdbTimestampField(e.target.value)}
                      placeholder="auto"
                    />
                    <small style={{ display: 'block', marginTop: '5px', color: darkMode ? '#94a3b8' : '#6b7280' }}>
                      Field used for incremental sync (default: auto-detect from updatedAt, createdAt, etc.)
                    </small>
                  </label>
                )}
                </div>

                <div style={{ 
                  marginTop: '20px', 
                  padding: '20px', 
                  backgroundColor: darkMode ? '#1e293b' : '#f3f4f6', 
                  borderRadius: '8px',
                  border: darkMode ? '1px solid #334155' : '1px solid #e5e7eb',
                  width: '100%',
                  boxSizing: 'border-box'
                }}>
                  <h4 style={{ 
                    marginTop: 0, 
                    marginBottom: '15px',
                    fontSize: '16px',
                    fontWeight: 'bold',
                    color: darkMode ? '#e2e8f0' : '#1f2937'
                  }}>
                    ⚙️ Advanced Options
                  </h4>
                  
                  <label style={{ 
                    display: 'flex', 
                    alignItems: 'flex-start', 
                    marginBottom: '15px', 
                    cursor: 'pointer',
                    padding: '10px',
                    backgroundColor: darkMode ? '#0f172a' : '#ffffff',
                    borderRadius: '6px',
                    border: darkMode ? '1px solid #334155' : '1px solid #e5e7eb',
                    width: '100%',
                    boxSizing: 'border-box'
                  }}>
                    <input
                      type="checkbox"
                      checked={docdbMatchIndexNames}
                      onChange={(e) => setDocdbMatchIndexNames(e.target.checked)}
                      style={{ 
                        marginRight: '12px',
                        marginTop: '3px',
                        flexShrink: 0,
                        width: 'auto',
                        height: 'auto',
                        padding: 0
                      }}
                    />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ 
                        fontWeight: '600',
                        marginBottom: '4px',
                        color: darkMode ? '#e2e8f0' : '#111827',
                        wordWrap: 'break-word',
                        whiteSpace: 'normal',
                        lineHeight: '1.5'
                      }}>
                        Match Index Names
                      </div>
                      <div style={{ 
                        fontSize: '13px',
                        color: darkMode ? '#94a3b8' : '#6b7280',
                        wordWrap: 'break-word',
                        whiteSpace: 'normal',
                        lineHeight: '1.5'
                      }}>
                        Recreate indexes to match DocumentDB names exactly
                      </div>
                    </div>
                  </label>

                  <label style={{ 
                    display: 'flex', 
                    alignItems: 'flex-start', 
                    marginBottom: '15px', 
                    cursor: 'pointer',
                    padding: '10px',
                    backgroundColor: darkMode ? '#0f172a' : '#ffffff',
                    borderRadius: '6px',
                    border: darkMode ? '1px solid #334155' : '1px solid #e5e7eb',
                    width: '100%',
                    boxSizing: 'border-box'
                  }}>
                    <input
                      type="checkbox"
                      checked={docdbDeleteLocalAfter}
                      onChange={(e) => setDocdbDeleteLocalAfter(e.target.checked)}
                      style={{ 
                        marginRight: '12px',
                        marginTop: '3px',
                        flexShrink: 0,
                        width: 'auto',
                        height: 'auto',
                        padding: 0
                      }}
                    />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ 
                        fontWeight: '600',
                        marginBottom: '4px',
                        color: darkMode ? '#e2e8f0' : '#111827',
                        wordWrap: 'break-word',
                        whiteSpace: 'normal',
                        lineHeight: '1.5'
                      }}>
                        Delete Local Dump After Migration
                      </div>
                      <div style={{ 
                        fontSize: '13px',
                        color: darkMode ? '#94a3b8' : '#6b7280',
                        wordWrap: 'break-word',
                        whiteSpace: 'normal',
                        lineHeight: '1.5'
                      }}>
                        Automatically clean up local dump files after successful migration
                      </div>
                    </div>
                  </label>

                  <label style={{ 
                    display: 'flex', 
                    alignItems: 'flex-start', 
                    cursor: 'pointer',
                    padding: '10px',
                    backgroundColor: darkMode ? '#0f172a' : '#ffffff',
                    borderRadius: '6px',
                    border: darkMode ? '1px solid #334155' : '1px solid #e5e7eb',
                    width: '100%',
                    boxSizing: 'border-box'
                  }}>
                    <input
                      type="checkbox"
                      checked={docdbDryRun}
                      onChange={(e) => setDocdbDryRun(e.target.checked)}
                      style={{ 
                        marginRight: '12px',
                        marginTop: '3px',
                        flexShrink: 0,
                        width: 'auto',
                        height: 'auto',
                        padding: 0
                      }}
                    />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ 
                        fontWeight: '600',
                        marginBottom: '4px',
                        color: darkMode ? '#e2e8f0' : '#111827',
                        wordWrap: 'break-word',
                        whiteSpace: 'normal',
                        lineHeight: '1.5'
                      }}>
                        Dry Run
                      </div>
                      <div style={{ 
                        fontSize: '13px',
                        color: darkMode ? '#94a3b8' : '#6b7280',
                        wordWrap: 'break-word',
                        whiteSpace: 'normal',
                        lineHeight: '1.5'
                      }}>
                        Validate configuration without executing any destructive operations
                      </div>
                    </div>
                  </label>
                </div>
              </>
            )}

            {docdbAction === 'init_last_run' && (
              <div style={{
                marginBottom: '20px',
                paddingBottom: '20px',
                borderBottom: darkMode ? '2px solid #334155' : '2px solid #e5e7eb'
              }}>
                <h3 style={{
                  fontSize: '16px',
                  fontWeight: 'bold',
                  marginBottom: '15px',
                  color: darkMode ? '#e2e8f0' : '#111827',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '8px'
                }}>
                  <span>🕐</span> Timestamp Initialization
                </h3>
                <label>
                  Initialize From
                  <select value={docdbInitSource} onChange={(e) => setDocdbInitSource(e.target.value)}>
                    <option value="atlas">Atlas (recommended after first migration)</option>
                    <option value="docdb">DocumentDB</option>
                  </select>
                  <small style={{ display: 'block', marginTop: '5px', color: darkMode ? '#94a3b8' : '#6b7280' }}>
                    Scan the selected cluster to find the latest timestamp and initialize the state file
                  </small>
                </label>
              </div>
            )}

            <div style={{
              marginTop: '30px',
              paddingTop: '20px',
              borderTop: darkMode ? '2px solid #334155' : '2px solid #e5e7eb',
              display: 'flex',
              justifyContent: 'center'
            }}>
              <button
                onClick={async () => {
                  setDocdbLoading(true);
                  setDocdbLogs('');
                  setDocdbError('');
                  setDocdbArtifacts([]);

                  try {
                    const payload = {
                      task_id: 'docdb_to_atlas_migration',
                      data: {
                        atlas_uri: docdbAtlasUri,
                        docdb_uri: docdbDocdbUri,
                        mode: docdbMode,
                        action: docdbAction,
                        databases: docdbDatabases,
                        num_workers: docdbNumWorkers,
                        num_parallel_collections: docdbNumParallelCollections,
                        timestamp_field: docdbTimestampField,
                        match_index_names: docdbMatchIndexNames,
                        delete_local_after: docdbDeleteLocalAfter,
                        dry_run: docdbDryRun,
                        init_source: docdbInitSource,
                      },
                    };

                    const result = await runStreamingTask('/api/tasks/run-stream/', payload, (msg) => {
                      setDocdbLogs((prev) => prev + msg + '\n');
                    });

                    if (result.artifacts) {
                      setDocdbArtifacts(createDownloadEntries(result.artifacts));
                    }
                    if (result.message) {
                      setDocdbLogs((prev) => prev + '\n✅ ' + result.message + '\n');
                    }
                  } catch (err) {
                    setDocdbError(err.message || String(err));
                    if (err.logs) {
                      setDocdbLogs(err.logs.join('\n'));
                    }
                  } finally {
                    setDocdbLoading(false);
                  }
                }}
                disabled={docdbLoading || !docdbAtlasUri || !docdbDocdbUri}
                style={{
                  padding: '14px 32px',
                  backgroundColor: docdbLoading || !docdbAtlasUri || !docdbDocdbUri ? '#9ca3af' : '#3b82f6',
                  color: 'white',
                  border: 'none',
                  borderRadius: '8px',
                  cursor: docdbLoading || !docdbAtlasUri || !docdbDocdbUri ? 'not-allowed' : 'pointer',
                  fontSize: '16px',
                  fontWeight: 'bold',
                  boxShadow: docdbLoading || !docdbAtlasUri || !docdbDocdbUri ? 'none' : '0 4px 6px rgba(59, 130, 246, 0.3)',
                  transition: 'all 0.2s ease',
                  minWidth: '200px'
                }}
              >
                {docdbLoading ? '⏳ Running...' : 
                  docdbAction === 'migrate' ? '🚀 Start Migration' :
                  docdbAction === 'init_last_run' ? '🔄 Initialize Timestamp' :
                  '📊 Estimate'}
              </button>
            </div>

            {docdbLogs && (
              <div style={{ marginTop: '20px' }}>
                <h3>Migration Logs</h3>
                <pre style={{
                  padding: '15px',
                  borderRadius: '6px',
                  maxHeight: '400px',
                  overflow: 'auto',
                  fontSize: '13px',
                  lineHeight: '1.5'
                }}>
                  {docdbLogs}
                </pre>
              </div>
            )}

            {docdbError && (
              <div style={{
                marginTop: '20px',
                padding: '15px',
                backgroundColor: darkMode ? '#7f1d1d' : '#fee2e2',
                border: darkMode ? '1px solid #991b1b' : '1px solid #fecaca',
                borderRadius: '6px',
                color: darkMode ? '#fecaca' : '#991b1b'
              }}>
                <h4 style={{ marginTop: 0 }}>❌ Error</h4>
                <p style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}>{docdbError}</p>
              </div>
            )}

            {docdbArtifacts.length > 0 && (
              <div style={{ marginTop: '20px' }}>
                <h3>📦 Download Migration Artifacts</h3>
                {docdbArtifacts.map((item, idx) => (
                  <div key={idx} style={{ marginBottom: '10px' }}>
                    <a href={item.url} download={item.filename} className="download-link">
                      💾 Download {item.filename}
                    </a>
                  </div>
                ))}
              </div>
            )}
          </fieldset>
        </motion.div>
      )}
    </div>
  );
};

const App = () => {
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.5 }}
    >
      <Dashboard />
      <Chatbot />
    </motion.div>
  );
};

export default App;
