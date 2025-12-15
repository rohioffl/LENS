import { useEffect, useMemo, useRef, useState } from 'react';

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

const App = () => {
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
  const [ecsTfSkipValidate, setEcsTfSkipValidate] = useState(true);
  const [ecsTfLogs, setEcsTfLogs] = useState('');
  const [ecsTfError, setEcsTfError] = useState('');
  const [ecsTfArtifacts, setEcsTfArtifacts] = useState([]);
  const [ecsManifestNamespace, setEcsManifestNamespace] = useState('');
  const [ecsManifestAwsMode, setEcsManifestAwsMode] = useState('auto');
  const [ecsManifestModel, setEcsManifestModel] = useState('gemini-1.5-flash');
  const [ecsManifestFallbacks, setEcsManifestFallbacks] = useState('');
  const [ecsManifestApiKey, setEcsManifestApiKey] = useState('');
  const [ecsManifestLogs, setEcsManifestLogs] = useState('');
  const [ecsManifestError, setEcsManifestError] = useState('');
  const [ecsManifestArtifacts, setEcsManifestArtifacts] = useState([]);

  useArtifactCleanup(tfArtifacts);
  useArtifactCleanup(invArtifacts);
  useArtifactCleanup(haArtifacts);
  useArtifactCleanup(classicArtifacts);
  useArtifactCleanup(ecrArtifacts);
  useArtifactCleanup(ecsTfArtifacts);
  useArtifactCleanup(ecsManifestArtifacts);

  const resolvedRegion = awsRegion === 'custom' ? customRegion.trim() : awsRegion;
  const authReady = Boolean(awsAccess.trim() && awsSecret.trim() && resolvedRegion);

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
        setEcsTerraformServices((prev) => (prev.length ? prev.filter((svc) => items.includes(svc)) : items));
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

  const toggleTerraformService = (service) => {
    setEcsTerraformServices((prev) =>
      prev.includes(service) ? prev.filter((item) => item !== service) : [...prev, service]
    );
  };

  const toggleManifestService = (service) => {
    setEcsManifestServices((prev) =>
      prev.includes(service) ? prev.filter((item) => item !== service) : [...prev, service]
    );
  };

  const selectAllTerraformServices = () => setEcsTerraformServices(ecsServices);
  const clearTerraformServices = () => setEcsTerraformServices([]);
  const selectAllManifestServices = () => setEcsManifestServices(ecsServices);
  const clearManifestServices = () => setEcsManifestServices([]);

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
        skip_terraform_validate: ecsTfSkipValidate,
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
        namespace: ecsManifestNamespace.trim(),
        services: ecsManifestServices,
        aws_credentials_mode: ecsManifestAwsMode,
        gemini_model: ecsManifestModel.trim(),
        gemini_fallbacks: ecsManifestFallbacks.trim(),
        gemini_api_key: ecsManifestApiKey.trim(),
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

  const currentInvLogText = invLogs || (invLoading ? 'Inventory is running, waiting for server logs...' : 'Logs will appear here once a run starts.');

  return (
    <div>
      <h1>Lens Backend Demo</h1>
      <p>Select an automation task to get started.</p>

      {view === 'home' && (
      <div className="card-grid">
        <div className="task-card">
          <h2>VPC Terraform Toolkit</h2>
          <p>Convert AWS VPCs into GCP VPCs ready Terraform bundles with per-subnet overrides.</p>
          <button onClick={() => setView('terraform')}>Open Toolkit</button>
          </div>
          <div className="task-card">
            <h2>AWS Inventory Export</h2>
            <p>Create XLSX-based resource inventories directly from your browser.</p>
            <button onClick={() => setView('inventory')}>Run Inventory</button>
          </div>
        <div className="task-card">
          <h2>HA VPN Builder</h2>
          <p>Design a redundant AWS &lt;-&gt; GCP HA VPN with dual tunnels and BGP routing.</p>
          <button onClick={() => setView('ha_vpn')}>Plan HA VPN</button>
        </div>
        <div className="task-card">
          <h2>Classic VPN Builder</h2>
          <p>Provision single-tunnel AWS &lt;-&gt; GCP Classic VPN with BGP and IKE version selection.</p>
          <button onClick={() => setView('classic_vpn')}>Plan Classic VPN</button>
        </div>
        <div className="task-card">
          <h2>ECR to Artifact Registry</h2>
          <p>Migrate all ECR repos to GCP Artifact Registry with parallel pushes and skip existing tags.</p>
          <button onClick={() => setView('ecr_migration')}>Migrate Repos</button>
          </div>
        <div className="task-card">
          <h2>ECS → GKE Terraform</h2>
          <p>Size a regional GKE cluster from ECS services and download Terraform bundles.</p>
          <button onClick={() => setView('ecs_terraform')}>Plan Cluster</button>
        </div>
        <div className="task-card">
          <h2>ECS → GKE Manifests</h2>
          <p>Convert ECS task definitions into curated Kubernetes manifests using Gemini.</p>
          <button onClick={() => setView('ecs_manifests')}>Generate Manifests</button>
        </div>
        </div>
      )}

      {view !== 'home' && (
        <div className="view-nav">
          <button onClick={() => setView('home')}>← Back to task list</button>
        </div>
      )}

      {(['terraform', 'inventory', 'ha_vpn', 'classic_vpn', 'ecr_migration', 'ecs_terraform', 'ecs_manifests'].includes(view)) && (
      <fieldset>
        <legend>AWS Credentials & Region</legend>
        <label>
          AWS Access Key ID
          <input value={awsAccess} onChange={(e) => setAwsAccess(e.target.value)} placeholder="AKIA..." />
        </label>
        <label>
          AWS Secret Access Key
          <input type="password" value={awsSecret} onChange={(e) => setAwsSecret(e.target.value)} placeholder="••••" />
        </label>
        {['terraform', 'ha_vpn', 'classic_vpn', 'ecr_migration', 'ecs_terraform', 'ecs_manifests'].includes(view) && (
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
      </fieldset>
      )}

      {view === 'terraform' && (
      <fieldset>
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
      </fieldset>
      )}

      {view === 'terraform' && (
      <fieldset>
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
      </fieldset>
      )}

      {view === 'ecs_terraform' && (
      <>
      <fieldset>
        <legend>ECS Cluster & Target</legend>
        <label>
          ECS Cluster
          <input
            list="ecsClusterOptions"
            value={ecsClusterName}
            onChange={(e) => setEcsClusterName(e.target.value)}
            placeholder="my-ecs-cluster"
          />
          {ecsClusters.length > 0 && (
            <datalist id="ecsClusterOptions">
              {ecsClusters.map((name) => (
                <option key={name} value={name} />
              ))}
            </datalist>
          )}
        </label>
        {ecsClusterLoading && <small>Loading ECS clusters…</small>}
        {ecsClusterError && <div className="error">{ecsClusterError}</div>}
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
        <label className="checkbox-row">
          <input
            type="checkbox"
            checked={ecsTfSkipValidate}
            onChange={(e) => setEcsTfSkipValidate(e.target.checked)}
          />
          <span>Skip terraform init/validate (recommended if Terraform is not installed)</span>
        </label>
      </fieldset>
      <fieldset>
        <legend>ECS Services & Output</legend>
        <div className="aws-subnet-selector">
          <div className="label-row">
            <label>ECS Services</label>
            <div className="pill-actions">
              <button type="button" onClick={selectAllTerraformServices} disabled={!ecsServices.length}>
                Select all
              </button>
              <button type="button" onClick={clearTerraformServices} disabled={!ecsTerraformServices.length}>
                Clear
              </button>
            </div>
          </div>
          {ecsServicesLoading && <small>Loading ECS services…</small>}
          {ecsServicesError && <div className="error">{ecsServicesError}</div>}
          {!ecsServices.length && !ecsServicesLoading && <small>No ECS services detected for this cluster.</small>}
          <div className="checkbox-grid">
            {ecsServices.map((service) => (
              <label key={service} className="checkbox-item">
                <input
                  type="checkbox"
                  checked={ecsTerraformServices.includes(service)}
                  onChange={() => toggleTerraformService(service)}
                />
                <span>{service}</span>
              </label>
            ))}
          </div>
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

      {view === 'ecs_manifests' && (
      <>
      <fieldset>
        <legend>ECS Cluster & Services</legend>
        <label>
          ECS Cluster
          <input
            list="ecsClusterOptionsManifest"
            value={ecsClusterName}
            onChange={(e) => setEcsClusterName(e.target.value)}
            placeholder="my-ecs-cluster"
          />
          {ecsClusters.length > 0 && (
            <datalist id="ecsClusterOptionsManifest">
              {ecsClusters.map((name) => (
                <option key={name} value={name} />
              ))}
            </datalist>
          )}
        </label>
        {ecsClusterLoading && <small>Loading ECS clusters…</small>}
        {ecsClusterError && <div className="error">{ecsClusterError}</div>}
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
          {ecsServicesLoading && <small>Loading ECS services…</small>}
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
        <label>
          Kubernetes Namespace (optional)
          <input value={ecsManifestNamespace} onChange={(e) => setEcsManifestNamespace(e.target.value)} placeholder="team-namespace" />
        </label>
      </fieldset>
      <fieldset>
        <legend>Gemini Generation</legend>
        <label>
          AWS Credential Placeholders
          <select value={ecsManifestAwsMode} onChange={(e) => setEcsManifestAwsMode(e.target.value)}>
            <option value="auto">Auto (prompt per service)</option>
            <option value="yes">Always inject placeholders</option>
            <option value="no">Do not inject AWS credentials</option>
          </select>
        </label>
        <label>
          Gemini Model (optional)
          <input value={ecsManifestModel} onChange={(e) => setEcsManifestModel(e.target.value)} placeholder="gemini-1.5-flash" />
        </label>
        <label>
          Gemini Fallback Models (comma-separated)
          <input value={ecsManifestFallbacks} onChange={(e) => setEcsManifestFallbacks(e.target.value)} placeholder="gemini-1.5-pro,gemini-1.0-pro" />
        </label>
        <label>
          Gemini API Key Override (optional)
          <input
            type="password"
            value={ecsManifestApiKey}
            onChange={(e) => setEcsManifestApiKey(e.target.value)}
            placeholder="AIza..."
          />
        </label>
        <div className="info-callout">
          Requires the google-generativeai SDK and a valid GEMINI_API_KEY. Supply the key here or set it on the backend host.
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

      {isVpnView && (
      <>
      <fieldset className={showGcpSubnetOverlay ? 'fieldset-overlay' : undefined}>
        <legend>{vpnLegendLabel} - GCP Credentials & Network</legend>
        {showGcpSubnetOverlay && (
          <div className="loading-overlay">
            <div>
              <strong>Loading GCP subnets…</strong>
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
      <fieldset>
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
                return;
              }
              setEcrServiceFileName(file.name);
              const reader = new FileReader();
              reader.onload = (evt) => {
                setEcrServiceKey(evt.target?.result?.toString() || '');
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
      </fieldset>
      )}

      {view === 'inventory' && (
      <fieldset>
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
      </fieldset>
      )}
    </div>
  );
};

export default App;
