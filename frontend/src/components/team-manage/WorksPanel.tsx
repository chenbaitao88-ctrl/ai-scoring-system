import React from 'react';

interface WorksPanelProps {
  worksStatus: any;
  uploadResult: any;
  uploadingWorks: boolean;
  namingExample: string;
  onBatchUploadWorks: (e: React.ChangeEvent<HTMLInputElement>) => void;
  onBatchUploadFolders: (e: React.ChangeEvent<HTMLInputElement>) => void;
}

const WorksPanel: React.FC<WorksPanelProps> = ({
  worksStatus,
  uploadResult,
  uploadingWorks,
  namingExample,
  onBatchUploadWorks,
  onBatchUploadFolders,
}) => {
  return (
    <div className="bg-white p-6 rounded-lg shadow-sm space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-base font-medium text-gray-800">作品管理</h3>
        <div className="flex gap-3">
          <label className="px-4 py-2 bg-orange-600 text-white rounded cursor-pointer hover:bg-orange-700">
            {uploadingWorks ? '上传中...' : '批量上传ZIP'}
            <input
              type="file"
              accept=".zip"
              multiple
              className="hidden"
              onChange={onBatchUploadWorks}
              disabled={uploadingWorks}
            />
          </label>
          <label className="px-4 py-2 bg-teal-600 text-white rounded cursor-pointer hover:bg-teal-700">
            {uploadingWorks ? '上传中...' : '上传文件夹'}
            <input
              type="file"
              // @ts-ignore - webkitdirectory is not in the type definition
              webkitdirectory=""
              directory=""
              multiple
              className="hidden"
              onChange={onBatchUploadFolders}
              disabled={uploadingWorks}
            />
          </label>
        </div>
      </div>

      {/* 命名规则提示 */}
      <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-3 text-sm text-yellow-800">
        <strong>命名规则：</strong>
        <span className="font-mono">{namingExample}</span>（请参考当前赛事命名示例）
      </div>

      {/* 作品状态统计 */}
      {worksStatus && (
        <div className="grid grid-cols-4 gap-4">
          <div className="bg-blue-50 p-4 rounded-lg">
            <div className="text-2xl font-bold text-blue-600">{worksStatus.upload_progress}</div>
            <div className="text-sm text-blue-500">作品上传进度</div>
          </div>
          <div className="bg-green-50 p-4 rounded-lg">
            <div className="text-2xl font-bold text-green-600">{worksStatus.works_parsed}</div>
            <div className="text-sm text-green-500">已解析作品</div>
          </div>
          <div className="bg-purple-50 p-4 rounded-lg">
            <div className="text-2xl font-bold text-purple-600">{worksStatus.works_with_source}</div>
            <div className="text-sm text-purple-500">含源代码</div>
          </div>
          <div className="bg-orange-50 p-4 rounded-lg">
            <div className="text-2xl font-bold text-orange-600">{worksStatus.works_with_aigc}</div>
            <div className="text-sm text-orange-500">含AIGC日志</div>
          </div>
        </div>
      )}

      {/* 详细统计 */}
      {worksStatus && (
        <div className="grid grid-cols-3 gap-3 text-sm">
          <div className="flex justify-between px-3 py-2 bg-gray-50 rounded">
            <span className="text-gray-500">总队伍数</span>
            <span className="font-medium">{worksStatus.total_teams}</span>
          </div>
          <div className="flex justify-between px-3 py-2 bg-gray-50 rounded">
            <span className="text-gray-500">已上传作品</span>
            <span className="font-medium">{worksStatus.works_uploaded}</span>
          </div>
          <div className="flex justify-between px-3 py-2 bg-gray-50 rounded">
            <span className="text-gray-500">含截图/视频</span>
            <span className="font-medium">{worksStatus.works_with_screenshots}</span>
          </div>
          <div className="flex justify-between px-3 py-2 bg-gray-50 rounded">
            <span className="text-gray-500">异常标记</span>
            <span className="font-medium text-red-500">{worksStatus.works_flagged}</span>
          </div>
        </div>
      )}

      {/* 上传结果 */}
      {uploadResult && (
        <div className="border border-gray-200 rounded-lg p-4">
          <h4 className="text-sm font-medium text-gray-700 mb-2">
            上次上传结果：成功 {uploadResult.success} / 失败 {uploadResult.failed} / 总计 {uploadResult.total}
          </h4>
          {uploadResult.details && uploadResult.details.length > 0 && (
            <div className="max-h-48 overflow-y-auto space-y-1">
              {uploadResult.details.map((detail: any, idx: number) => (
                <div key={idx} className="flex items-center gap-2 text-sm">
                  {detail.error ? (
                    <span className="text-red-500">✗</span>
                  ) : detail.matched ? (
                    <span className="text-green-500">✓</span>
                  ) : (
                    <span className="text-yellow-500">?</span>
                  )}
                  <span className="text-gray-600 truncate flex-1">{detail.filename}</span>
                  {detail.error && <span className="text-red-500 text-xs">{detail.error}</span>}
                  {detail.matched === false && <span className="text-yellow-500 text-xs">未匹配队伍</span>}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default WorksPanel;
