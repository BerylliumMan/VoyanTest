import React from 'react';
import { Redirect } from 'react-router-dom';

/** Legacy /testcases → UI automation list */
const TestCasesRedirect: React.FC = () => <Redirect to="/testcases/ui" />;

export default TestCasesRedirect;
