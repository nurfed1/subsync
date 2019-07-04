import subsync.gui.layout.syncwin
import wx
from subsync.synchro import Synchronizer
from subsync.gui.components import filedlg
from subsync.gui import fpswin
from subsync.gui import errorwin
from subsync.gui import busydlg
from subsync.gui.components.thread import gui_thread
from subsync.data import filetypes
from subsync import subtitle
from subsync.settings import settings
from subsync import utils
from subsync import img
from subsync import error
import gizmo
import pysubs2.exceptions
import os

import logging
logger = logging.getLogger(__name__)


class SyncWin(subsync.gui.layout.syncwin.SyncWin):
    def __init__(self, parent, task, mode=None, refCache=None):
        super().__init__(parent)

        self.m_buttonDebugMenu.SetLabel(u'\u22ee') # 2630
        img.setItemBitmap(self.m_bitmapTick, 'tickmark')
        img.setItemBitmap(self.m_bitmapCross, 'crossmark')

        if settings().debugOptions:
            self.m_buttonDebugMenu.Show()

        self.m_buttonStop.SetFocus()
        self.Fit()
        self.Layout()

        self.errors = error.ErrorsCollector()
        self.pendingErrors = False

        self.task = task
        self.mode = mode
        self.outSaved = False

        self.sync = Synchronizer(task.sub, task.ref)
        self.sync.refCache = refCache
        self.sync.onError = self.onError

        self.running = True
        self.closing = False
        self.runTime = wx.StopWatch()

        self.sleeper = gizmo.Sleeper()
        self.thread = gizmo.Thread(self.syncJob, name='Synchro')

    def stop(self):
        self.running = False
        self.sleeper.wake()

    def syncJob(self):
        try:
            self.sync.onError = self.onError
            self.sync.init(runCb=lambda: self.running)
            if self.running:
                self.sync.start()
            self.updateStatusStarted()

            while self.running and self.sync.isRunning():
                self.updateStatus(self.sync.getStatus())
                self.sleeper.sleep(0.5)
        except Exception as err:
            logger.warning('%r', err, exc_info=True)
            self.onError('core', err)

        try:
            self.sync.stop()
        except Exception as err:
            logger.warning('%r', err, exc_info=True)
            self.onError('core', err)
        finally:
            self.updateStatusDone(self.sync.getStatus(), self.running)
            self.running = False
            self.sync.destroy()
            logger.info('thread terminated')

    @gui_thread
    def updateStatusStarted(self):
        self.m_textStatus.SetLabel(_('Synchronizing...'))

    @gui_thread
    def updateStatus(self, status, finished=False):
        elapsed = self.runTime.Time() / 1000
        self.m_textElapsedTime.SetLabel(utils.timeStampFmt(elapsed))

        if status:
            self.m_textSync.SetLabel(_('Synchronization: {} points').format(status.points))
            self.m_textCorrelation.SetLabel('{:.2f} %'.format(100 * status.factor))
            self.m_textFormula.SetLabel(str(status.formula))
            self.m_textMaxChange.SetLabel(utils.timeStampFractionFmt(status.maxChange))
            if finished:
                self.m_gaugeProgress.SetValue(100)
            else:
                self.m_gaugeProgress.SetValue(100 * status.progress)

            if status.subReady and not self.m_bitmapTick.IsShown():
                self.m_bitmapCross.Hide()
                self.m_bitmapTick.Show()
                self.m_buttonSave.Enable()
                if status.running:
                    self.m_textInitialSyncInfo.Show()
                self.Fit()
                self.Layout()

        self.handleOutput(status)
        self.updateStatusErrors()

    @gui_thread
    def updateStatusDone(self, status=None, finished=False):
        self.runTime.Pause()
        self.showCloseButton()

        if status:
            self.updateStatus(status, finished)

        if status and status.subReady:
            self.m_buttonSave.Enable()
            self.m_bitmapTick.Show()
            self.m_bitmapCross.Hide()
            if abs(status.maxChange) > 0.5:
                self.m_textStatus.SetLabel(_('Subtitles synchronized'))
            else:
                self.m_textStatus.SetLabel(_('No need to synchronize'))
        else:
            self.m_bitmapTick.Hide()
            self.m_bitmapCross.Show()
            if (finished and status.points > settings().minPointsNo/2 and
                    status.factor > settings().minCorrelation**10 and
                    status.maxDistance < 2*settings().maxPointDist):
                self.m_buttonSave.Enable()
                self.m_textStatus.SetLabel(_('Synchronization inconclusive'))
            else:
                self.m_textStatus.SetLabel(_('Couldn\'t synchronize'))

        self.handleOutput(status)
        self.updateStatusErrors()

        self.Fit()
        self.Layout()

    def handleOutput(self, status, finished=False):
        if status and (finished or status.effort >= settings().minEffort):
            if self.task.out and not self.outSaved:
                try:
                    self.saveSynchronizedSubtitles(
                            path=self.task.getOutputPath(),
                            enc=self.task.out.enc,
                            fps=self.task.out.fps,
                            overwrite=self.task.out.overwrite)
                except Exception as err:
                    logger.warning('%r', err, exc_info=True)
                    self.onError('out', err)

                self.outSaved = True

            if self.mode and self.mode.autoClose:
                if self.IsModal():
                    self.EndModal(wx.ID_OK)
                else:
                    self.Close()

            elif self.mode and self.mode.autoStart:
                self.stop()
                self.showCloseButton()

    @gui_thread
    def onError(self, source, err):
        msg = errorwin.syncErrorToString(source, err)
        self.errors.add(msg, source, err)
        self.pendingErrors = True

    def updateStatusErrors(self):
        if self.pendingErrors:
            self.pendingErrors = False
            self.m_textErrorMsg.SetLabelText(self.errors.getMessages())
            if not self.m_panelError.IsShown():
                self.m_panelError.Show()
            self.Fit()
            self.Layout()

    def showCloseButton(self):
        if not self.m_buttonClose.IsShown():
            self.m_buttonStop.Disable()
            self.m_buttonStop.Hide()
            self.m_buttonClose.Enable()
            self.m_buttonClose.Show()
            self.m_textInitialSyncInfo.Hide()
            self.m_buttonClose.SetFocus()
            self.m_buttonSave.SetFocus()

            self.Fit()
            self.Layout()

    def ShowModal(self):
        res = super().ShowModal()
        self.onClose(None)  # since EVT_CLOSE is not emitted for modal frame
        return res

    def onClose(self, event):
        if not self.closing:
            self.closing = True
            self.stop()

            if self.thread.isRunning():
                with busydlg.BusyDlg(self, _('Terminating, please wait...')) as dlg:
                    dlg.ShowModalWhile(self.thread.isRunning)

        if event:
            event.Skip()

    def onButtonStopClick(self, event):
        self.stop()
        self.showCloseButton()

    @errorwin.error_dlg
    def onButtonSaveClick(self, event):
        path = self.saveFileDlg(self.task.ref.path)
        if path != None:
            try:
                self.saveSynchronizedSubtitles(path, overwrite=True)

            except pysubs2.exceptions.UnknownFPSError:
                with fpswin.FpsWin(self, self.task.sub.fps, self.task.ref.fps) as dlg:
                    if dlg.ShowModal() == wx.ID_OK:
                        self.saveSynchronizedSubtitles(path, fps=dlg.getFps(), overwrite=True)

    def saveSynchronizedSubtitles(self, path, enc=None, **kw):
        enc = enc or settings().outputCharEnc or self.task.sub.enc or 'UTF-8'
        self.sync.getSynchronizedSubtitles().save(path, encoding=enc, **kw)

    def onTextShowDetailsClick(self, event):
        self.m_panelDetails.Show()
        self.m_textShowDetails.Hide()
        self.Fit()
        self.Layout()

    def onTextHideDetailsClick(self, event):
        self.m_panelDetails.Hide()
        self.m_textShowDetails.Show()
        self.Fit()
        self.Layout()

    def onTextErrorDetailsClick(self, event):
        errorwin.showErrorDetailsDlg(self, self.errors.getDetails(), _('Error'))

    def saveFileDlg(self, path=None, suffix=None):
        props = {}
        filters = '|'.join('|'.join(x) for x in filetypes.subtitleTypes)
        props['wildcard'] = '{}|{}|*.*'.format(filters, _('All files'))
        props['defaultFile'] = self.genDefaultFileName(path, suffix)
        if path:
            props['defaultDir'] = os.path.dirname(path)
        return filedlg.showSaveFileDlg(self, **props)

    def genDefaultFileName(self, path, suffix=None):
        try:
            res = []
            basename, _ = os.path.splitext(os.path.basename(path))
            res.append(basename)

            if suffix:
                res.append(suffix)

            elif settings().appendLangCode and self.task.sub.lang:
                res.append(self.task.sub.lang)

            res.append('srt')
            return '.'.join(res)
        except Exception as e:
            logger.warning('%r', e)


    ##### DEBUG UTILS #####

    def onButtonDebugMenuClick(self, event):
        self.PopupMenu(self.m_menuDebug)

    def onMenuItemEnableSaveClick(self, event):
        self.m_buttonSave.Enable()

    @errorwin.error_dlg
    def onMenuItemDumpSubWordsClick(self, event):
        self.saveWordsDlg(self.task.sub, self.sync.correlator.getSubs())

    @errorwin.error_dlg
    def onMenuItemDumpRefWordsClick(self, event):
        self.saveWordsDlg(self.task.ref, self.sync.correlator.getRefs())

    def saveWordsDlg(self, stream, words):
        subs = subtitle.Subtitles()
        for word in words:
            subs.add(word.time, word.time, word.text)

        suffix = 'words'
        if stream.lang:
            suffix += '.' + stream.lang

        path = self.saveFileDlg(stream.path, suffix=suffix)
        if path != None:
            fps = self.task.sub.fps if self.task.sub.fps != None else self.task.ref.fps
            subs.save(path, fps=fps)

    @errorwin.error_dlg
    def onMenuItemDumpAllSyncPointsClick(self, event):
        self.saveSyncPoints(self.sync.correlator.getAllPoints())

    def onMenuItemDumpUsedSyncPointsClick(self, event):
        self.saveSyncPoints(self.sync.correlator.getUsedPoints())

    def saveSyncPoints(self, pts):
        wildcard = '*.csv|*.csv|{}|*.*'.format(_('All files'))
        path = filedlg.showSaveFileDlg(self, wildcard=wildcard)
        if path:
            with open(path, 'w') as fp:
                for x, y in pts:
                    fp.write('{:.3f},{:.3f}\n'.format(x, y))
