import defaultSettings from '../settings.json';
export interface GlobalState {
  settings?: typeof defaultSettings;
  userInfo?: {
    name?: string;
    avatar?: string;
    job?: string;
    organization?: string;
    location?: string;
    email?: string;
    permissions: Record<string, string[]>;
    must_change_password?: boolean;
  };
  userLoading?: boolean;
}

export type GlobalAction =
  | {
      type: 'update-settings';
      payload: { settings: GlobalState['settings'] };
    }
  | {
      type: 'update-userInfo';
      payload: {
        userInfo?: GlobalState['userInfo'];
        userLoading?: boolean;
      };
    };

const initialState: GlobalState = {
  settings: defaultSettings,
  userInfo: {
    permissions: {},
  },
};

export default function store(
  state: GlobalState = initialState,
  action: GlobalAction
): GlobalState {
  switch (action.type) {
    case 'update-settings': {
      const { settings } = action.payload;
      return {
        ...state,
        settings,
      };
    }
    case 'update-userInfo': {
      const { userInfo = initialState.userInfo, userLoading } = action.payload;
      return {
        ...state,
        userLoading,
        userInfo,
      };
    }
    default:
      return state;
  }
}
