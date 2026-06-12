import { useEffect, useState, useCallback } from 'react';
import axios from 'axios';
import { TestCase, Module, Environment } from '../types';

export default function useTestCaseData(
  selectedProject: number | null,
  selectedModuleId: number | null,
  page: number,
  pageSize: number,
  submittedQuery: string,
) {
  const [data, setData] = useState<TestCase[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [modules, setModules] = useState<Module[]>([]);
  const [moduleTree, setModuleTree] = useState<Module[]>([]);
  const [environments, setEnvironments] = useState<Environment[]>([]);
  const [selectedEnvironment, setSelectedEnvironment] = useState<number | null>(null);

  const fetchData = useCallback(() => {
    if (!selectedProject) return;
    setLoading(true);
    if (submittedQuery) {
      axios.get(`/api/testcases/search?project_id=${selectedProject}&q=${encodeURIComponent(submittedQuery)}&page=${page}&size=${pageSize}`)
        .then((res) => { setData(res.data.items || []); setTotal(res.data.total_items || 0); })
        .catch(console.error)
        .finally(() => setLoading(false));
      return;
    }
    const url = selectedModuleId
      ? `/api/testcases/module/${selectedModuleId}/testcases?page=${page}&size=${pageSize}`
      : `/api/testcases/project/${selectedProject}/testcases?page=${page}&size=${pageSize}`;
    axios.get(url)
      .then((res) => { setData(res.data.items || []); setTotal(res.data.total_items || 0); })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [selectedProject, selectedModuleId, page, pageSize, submittedQuery]);

  const fetchModules = useCallback(() => {
    if (!selectedProject) return;
    axios.get(`/api/projects/${selectedProject}/modules`).then((res) => {
      setModules(res.data || []);
    }).catch(console.error);
    axios.get(`/api/projects/${selectedProject}/modules/tree`).then((res) => {
      setModuleTree(res.data || []);
    }).catch(console.error);
  }, [selectedProject]);

  const fetchEnvironments = useCallback(() => {
    if (!selectedProject) {
      setEnvironments([]);
      setSelectedEnvironment(null);
      return;
    }
    axios.get(`/api/projects/${selectedProject}/environments`).then((res) => {
      const envs = res.data || [];
      setEnvironments(envs);
      const defaultEnv = envs.find((e: Environment) => e.is_default);
      setSelectedEnvironment(defaultEnv ? defaultEnv.id : (envs[0]?.id || null));
    }).catch(console.error);
  }, [selectedProject]);

  useEffect(() => { fetchData(); fetchModules(); fetchEnvironments(); }, [fetchData, fetchModules, fetchEnvironments]);

  return {
    data,
    total,
    loading,
    modules,
    moduleTree,
    environments,
    selectedEnvironment,
    setSelectedEnvironment,
    fetchData,
    fetchModules,
    fetchEnvironments,
  };
}
