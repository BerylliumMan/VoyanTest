import React from 'react';
import { Redirect } from 'react-router-dom';

/** Legacy /api_test → 接口定义页 */
const ApiTestRedirect: React.FC = () => <Redirect to="/api_test/overview" />;

export default ApiTestRedirect;
