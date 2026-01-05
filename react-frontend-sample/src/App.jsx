import { useEffect, useMemo, useRef, useState } from 'react';
import { motion } from 'framer-motion';

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
  'lambda',
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
  const [awsAccess, setAwsAccess] = useState('');
  const [awsSecret, setAwsSecret] = useState('');
  const [awsRegion, setAwsRegion] = useState(AWS_REGIONS[0].id);
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
  
  const [boxCloud, setBoxCloud] = useState('aws');
  const [boxAwsRegion, setBoxAwsRegion] = useState('ap-south-1');
  const [boxGcpProject, setBoxGcpProject] = useState('');
  const [boxGcpRegion, setBoxGcpRegion] = useState('us-central1');
  const [boxServiceOptions, setBoxServiceOptions] = useState([]);
  const [boxServiceSchemas, setBoxServiceSchemas] = useState({});
  const [boxSelectedServices, setBoxSelectedServices] = useState([]);
  const [boxServiceInputs, setBoxServiceInputs] = useState({});
  const [boxMetadataLoading, setBoxMetadataLoading] = useState(false);
  const [boxMetadataError, setBoxMetadataError] = useState('');
  const [boxArtifacts, setBoxArtifacts] = useState([]);
  const [boxLogs, setBoxLogs] = useState('');
  const [boxError, setBoxError] = useState('');

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
  useArtifactCleanup(boxArtifacts);

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

  useEffect(() => {
    if (view !== 'box_project') {
      setBoxServiceOptions([]);
      setBoxServiceSchemas({});
      setBoxSelectedServices([]);
      setBoxServiceInputs({});
      setBoxMetadataError('');
      setBoxMetadataLoading(false);
      return;
    }
    setBoxMetadataLoading(true);
    const timer = setTimeout(async () => {
      try {
        const res = await postJson('/api/box/metadata/', {
          cloud_provider: boxCloud,
        });
        const options = res.services || [];
        const schemas = res.inputs || {};
        setBoxServiceOptions(options);
        setBoxServiceSchemas(schemas);
        setBoxMetadataError('');
        setBoxSelectedServices((prev) => {
          const filtered = prev.filter((svc) => options.some((opt) => opt.id === svc));
          if (filtered.length) {
            ensureBoxServiceDefaults(filtered, schemas);
          }
          setBoxServiceInputs((prevInputs) => {
            const nextInputs = {};
            filtered.forEach((svc) => {
              if (prevInputs[svc]) {
                nextInputs[svc] = prevInputs[svc];
              }
            });
            return nextInputs;
          });
          return filtered;
        });
      } catch (err) {
        setBoxMetadataError(err.message || String(err));
        setBoxServiceOptions([]);
        setBoxServiceSchemas({});
        setBoxSelectedServices([]);
        setBoxServiceInputs({});
      } finally {
        setBoxMetadataLoading(false);
      }
    }, debounceDelay);
    return () => clearTimeout(timer);
  }, [view, boxCloud]);

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
  const getBoxServiceLabel = (service) =>
    boxServiceOptions.find((option) => option.id === service)?.label || service;
  const ensureBoxServiceDefaults = (serviceIds, schemaOverride) => {
    const schemaSource = schemaOverride || boxServiceSchemas;
    setBoxServiceInputs((prev) => {
      const next = { ...prev };
      serviceIds.forEach((svc) => {
        if (!next[svc]) {
          const schema = schemaSource?.[svc] || [];
          const defaults = {};
          schema.forEach((field) => {
            defaults[field.name] = field.default ?? '';
          });
          next[svc] = defaults;
        }
      });
      return next;
    });
  };
  const toggleBoxService = (service) => {
    setBoxSelectedServices((prev) => {
      const exists = prev.includes(service);
      const updated = exists ? prev.filter((item) => item !== service) : [...prev, service];
      if (!exists) {
        ensureBoxServiceDefaults([service]);
      }
      return updated;
    });
  };
  const selectAllBoxServices = () => {
    const all = boxServiceOptions.map((option) => option.id);
    setBoxSelectedServices(all);
    ensureBoxServiceDefaults(all);
  };
  const clearBoxServices = () => setBoxSelectedServices([]);
  const updateBoxServiceInput = (service, field, value) => {
    setBoxServiceInputs((prev) => ({
      ...prev,
      [service]: {
        ...(prev[service] || {}),
        [field]: value,
      },
    }));
  };

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

  const runBoxProjectTask = async () => {
    setBoxError('');
    setBoxLogs('Preparing Terraform project...\n');
    setBoxArtifacts([]);
    if (!boxSelectedServices.length) {
      setBoxError('Select at least one service.');
      return;
    }
    if (boxCloud === 'aws' && !boxAwsRegion.trim()) {
      setBoxError('AWS region is required.');
      return;
    }
    if (boxCloud === 'gcp' && (!boxGcpProject.trim() || !boxGcpRegion.trim())) {
      setBoxError('GCP project and region are required.');
      return;
    }
    const payload = {
      cloud_provider: boxCloud,
      services: boxSelectedServices,
      service_inputs: boxSelectedServices.reduce((acc, svc) => {
        if (boxServiceInputs[svc]) {
          acc[svc] = boxServiceInputs[svc];
        }
        return acc;
      }, {}),
    };
    if (boxCloud === 'aws') {
      payload.aws_region = boxAwsRegion.trim();
    } else {
      payload.gcp_project = boxGcpProject.trim();
      payload.gcp_region = boxGcpRegion.trim();
    }
    try {
      const event = await runStreamingTask(
        '/api/tasks/run-stream/',
        { task_id: 'box_project', data: payload },
        (message) => setBoxLogs((prev) => mergeBackendLogs(prev, message))
      );
      setBoxArtifacts(createDownloadEntries(event.artifacts || []));
    } catch (err) {
      setBoxError(err.message || String(err));
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
          custom={12}
          whileHover="hover"
          whileTap={{ scale: 0.98 }}
        >
          <h2>Box Terraform Generator</h2>
          <p>Assemble Terraform modules for popular AWS or GCP services in minutes.</p>
          <button onClick={() => setView('box_project')}>Build Project</button>
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

      {view === 'box_project' && (
      <>
      <fieldset>
        <legend>Cloud & Scope</legend>
        <label>
          Cloud Provider
          <select value={boxCloud} onChange={(e) => setBoxCloud(e.target.value)}>
            <option value="aws">AWS</option>
            <option value="gcp">GCP</option>
          </select>
        </label>
        {boxCloud === 'aws' ? (
          <label>
            AWS Region
            <input value={boxAwsRegion} onChange={(e) => setBoxAwsRegion(e.target.value)} placeholder="ap-south-1" />
          </label>
        ) : (
          <>
            <label>
              GCP Project ID
              <input value={boxGcpProject} onChange={(e) => setBoxGcpProject(e.target.value)} placeholder="my-gcp-project" />
            </label>
            <label>
              GCP Region
              <input value={boxGcpRegion} onChange={(e) => setBoxGcpRegion(e.target.value)} placeholder="us-central1" />
            </label>
          </>
        )}
      </fieldset>
      <fieldset>
        <legend>Services</legend>
        <div className="aws-subnet-selector">
          <div className="label-row">
            <label>Available Services</label>
            <div className="pill-actions">
              <button type="button" onClick={selectAllBoxServices} disabled={!boxServiceOptions.length}>
                Select all
              </button>
              <button type="button" onClick={clearBoxServices} disabled={!boxSelectedServices.length}>
                Clear
              </button>
            </div>
          </div>
          {boxMetadataLoading && <small>Loading service metadata...</small>}
          {boxMetadataError && <div className="error">{boxMetadataError}</div>}
          <div className="checkbox-grid">
            {boxServiceOptions.map((service) => (
              <label key={service.id} className="checkbox-item">
                <input
                  type="checkbox"
                  checked={boxSelectedServices.includes(service.id)}
                  onChange={() => toggleBoxService(service.id)}
                />
                <span>{service.label}</span>
              </label>
            ))}
          </div>
        </div>
      </fieldset>
      {!!boxSelectedServices.length && (
        <motion.fieldset
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5 }}
        >
          <legend>Service Parameters</legend>
          <div className="info-callout">
            Provide overrides for each module variable. Leave a field blank to use the default value suggested by box-project.
          </div>
          {boxSelectedServices.map((service) => (
            <div key={service} className="service-input-card">
              <h4>{getBoxServiceLabel(service)}</h4>
              {(boxServiceSchemas[service] || []).map((field) => (
                <label key={field.name}>
                  {field.prompt || field.name}
                  <input
                    value={(boxServiceInputs[service] || {})[field.name] ?? ''}
                    onChange={(e) => updateBoxServiceInput(service, field.name, e.target.value)}
                    placeholder={field.default ?? ''}
                  />
                </label>
              ))}
              {!boxServiceSchemas[service]?.length && <small>No additional inputs required.</small>}
            </div>
          ))}
        </motion.fieldset>
      )}
      <fieldset>
        <legend>Generate Terraform</legend>
        <button onClick={runBoxProjectTask} disabled={!boxSelectedServices.length}>
          Build Terraform Project
        </button>
        {boxError && <div className="error">{boxError}</div>}
        <h3>Logs</h3>
        <pre>{boxLogs || 'Logs will appear here once a run starts.'}</pre>
        <h3>Artifacts</h3>
        <div className="artifacts">
          {boxArtifacts.map((artifact) => (
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
    </motion.div>
  );
};

export default App;
