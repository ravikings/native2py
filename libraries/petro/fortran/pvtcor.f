C=======================================================================
C     PVTCOR   -   BLACK OIL PVT CORRELATION PACKAGE
C
C     WRITTEN   14-MAR-1988   R.T. HALSEY
C     REVISED   02-SEP-1991   VAZQUEZ-BEGGS ADDED AS CORRELATION 2
C     REVISED   19-MAY-1993   GLASO ADDED AS CORRELATION 3
C     REVISED   05-FEB-1996   FIXED DIVIDE BY ZERO WHEN API .LT. 5.0
C
C     ENTRY POINTS
C         PVTINI   INITIALISE CORRELATION SELECTION AND CONSTANTS
C         PVTBUB   BUBBLE POINT PRESSURE           (FUNCTION)
C         PVTRS    SOLUTION GAS OIL RATIO          (FUNCTION)
C         PVTBO    OIL FORMATION VOLUME FACTOR     (FUNCTION)
C         PVTVIS   OIL VISCOSITY (BEGGS-ROBINSON)  (FUNCTION)
C         PVTZ     GAS Z FACTOR (HALL-YARBOROUGH)  (FUNCTION)
C         PVTSET   FILL /PVTOUT/ FOR ONE PRESSURE  (SUBROUTINE)
C         PVTERR   REPORT AND CLEAR ERROR STATE    (SUBROUTINE)
C
C     ALL ROUTINES COMMUNICATE THROUGH /FLUID/ AND /PVTOUT/.  THE ONLY
C     ARGUMENT ANY OF THEM TAKES IS PRESSURE.  THIS IS INTENTIONAL AND
C     MATCHES THE 1988 CALLING CONVENTION USED BY SIMCOR AND WELLIB.
C=======================================================================
      BLOCK DATA PVTBLK
      INCLUDE 'PETRO.INC'
      COMMON /PVTSEL/ ICORRS, ICORBO, ICORVI, ICORZF
      DATA ICORRS /1/, ICORBO /1/, ICORVI /1/, ICORZF /1/
      DATA API /35.0D0/, SGG /0.65D0/, SGW /1.02D0/
      DATA TRES /180.0D0/, PB /0.0D0/
      DATA PSEP /114.7D0/, TSEP /80.0D0/, NCOMP /0/
      DATA IERR /0/, IWARN /0/, NITPVT /0/
      DATA LUNPRT /6/, LUNDBG /0/, IDBGLV /0/
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE PVTINI (GRAV, GASSG, TEMPF, ICORR)
C-----------------------------------------------------------------------
C     SET THE FLUID DESCRIPTION AND SELECT A CORRELATION FAMILY.
C     ICORR  1 = STANDING (1947)
C            2 = VAZQUEZ-BEGGS (1980)
C            3 = GLASO (1980)
C-----------------------------------------------------------------------
      INCLUDE 'PETRO.INC'
      COMMON /PVTSEL/ ICORRS, ICORBO, ICORVI, ICORZF
C
      API  = GRAV
      SGG  = GASSG
      TRES = TEMPF
      IERR = 0
      IWARN= 0
C
      IF (API .LT. 5.0D0 .OR. API .GT. 65.0D0) THEN
         IWARN = IWARN + 1
         IF (LUNPRT .GT. 0) WRITE (LUNPRT,9000) API
      END IF
      IF (API .LT. 1.0D0) THEN
C        ---- 05-FEB-1996: RHOO BELOW WOULD DIVIDE BY (131.5+API) ~ 0
         API  = 1.0D0
         IERR = -1
      END IF
      IF (SGG .LE. SMALL) THEN
         IERR = 1
         RETURN
      END IF
C
      IF (ICORR .LT. 1 .OR. ICORR .GT. 3) THEN
         ICORRS = 1
         IWARN  = IWARN + 1
      ELSE
         ICORRS = ICORR
      END IF
      ICORBO = ICORRS
      ICORVI = 1
      ICORZF = 1
C
      PB = PVTBUB(1000.0D0)
      RETURN
 9000 FORMAT (' *** PVTINI WARNING - API GRAVITY ',F8.2,
     &        ' OUTSIDE CORRELATION RANGE 5 - 65')
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION PVTBUB (RSTGT)
C-----------------------------------------------------------------------
C     BUBBLE POINT PRESSURE, PSIA, FOR A TARGET SOLUTION GOR RSTGT.
C     SOLVED BY NEWTON ON PVTRS SINCE THE STANDING FORM INVERTS
C     ANALYTICALLY BUT VAZQUEZ-BEGGS AND GLASO DO NOT.
C-----------------------------------------------------------------------
      INCLUDE 'PETRO.INC'
      COMMON /PVTSEL/ ICORRS, ICORBO, ICORVI, ICORZF
      DATA TOL /1.0D-6/, ITMAX /60/
C
      P = 1000.0D0
      DO 100 IT = 1, ITMAX
         F  = PVTRS(P) - RSTGT
         DP = MAX(ONE, 1.0D-4*P)
         FP = (PVTRS(P+DP) - RSTGT - F) / DP
         IF (ABS(FP) .LT. SMALL) GO TO 900
         STEP = F / FP
C        ---- DAMPING.  WITHOUT THIS THE 1988 VERSION WOULD OVERSHOOT
C        ---- TO NEGATIVE PRESSURE FOR HEAVY OIL AND LOOP TO ITMAX.
         IF (STEP .GT.  0.5D0*P) STEP =  0.5D0*P
         IF (STEP .LT. -2.0D0*P) STEP = -2.0D0*P
         P = P - STEP
         IF (P .LT. PATM) P = PATM
         NITPVT = IT
         IF (ABS(STEP) .LT. TOL*MAX(ONE,P)) GO TO 200
  100 CONTINUE
      IWARN = IWARN + 1
      IF (LUNPRT .GT. 0) WRITE (LUNPRT,9010) RSTGT, P
C
  200 PVTBUB = P
      RETURN
C
  900 IERR   = 2
      PVTBUB = PATM
      RETURN
 9010 FORMAT (' *** PVTBUB NOT CONVERGED FOR RS =',F10.2,
     &        '  LAST P =',F12.3)
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION PVTRS (P)
C-----------------------------------------------------------------------
C     SOLUTION GAS OIL RATIO, SCF/STB, AT PRESSURE P (PSIA).
C-----------------------------------------------------------------------
      INCLUDE 'PETRO.INC'
      COMMON /PVTSEL/ ICORRS, ICORBO, ICORVI, ICORZF
C
      IF (P .LE. PATM) THEN
         PVTRS = ZERO
         RETURN
      END IF
C
      GO TO (10, 20, 30), ICORRS
C
C     ---- 1: STANDING (1947)
   10 CONTINUE
      YY    = 0.00091D0*TRES - 0.0125D0*API
      PVTRS = SGG * ((P/18.2D0 + 1.4D0) * 10.0D0**(-YY))**1.2048D0
      RETURN
C
C     ---- 2: VAZQUEZ-BEGGS (1980), GRAVITY CORRECTED TO 100 PSIG SEP
   20 CONTINUE
      SGC = SGG * (ONE + 5.912D-5*API*TSEP*DLOG10(PSEP/114.7D0))
      IF (API .LE. 30.0D0) THEN
         C1 = 0.0362D0
         C2 = 1.0937D0
         C3 = 25.7240D0
      ELSE
         C1 = 0.0178D0
         C2 = 1.1870D0
         C3 = 23.9310D0
      END IF
      PVTRS = C1 * SGC * P**C2 * DEXP(C3*API/(TRES + TABS))
      RETURN
C
C     ---- 3: GLASO (1980)
   30 CONTINUE
      A     = 2.8869D0 - DSQRT(14.1811D0 - 3.3093D0*DLOG10(P))
      PSTAR = 10.0D0**A
      PVTRS = SGG * (PSTAR * API**0.989D0 / TRES**0.172D0)**1.2255D0
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION PVTBO (P)
C-----------------------------------------------------------------------
C     OIL FORMATION VOLUME FACTOR, RB/STB.
C     BELOW PB   -   STANDING / VAZQUEZ-BEGGS SATURATED FORM
C     ABOVE PB   -   UNDERSATURATED, COMPRESSED WITH CO
C-----------------------------------------------------------------------
      INCLUDE 'PETRO.INC'
      COMMON /PVTSEL/ ICORRS, ICORBO, ICORVI, ICORZF
C
      SGO = 141.5D0 / (131.5D0 + API)
      PS  = MIN(P, PB)
      RS  = PVTRS(PS)
C
      IF (ICORBO .EQ. 2) THEN
         BOS = ONE + 4.677D-4*RS
     &             + 1.751D-5*(TRES-60.0D0)*(API/SGG)
     &             - 1.811D-8*RS*(TRES-60.0D0)*(API/SGG)
      ELSE
         FF  = RS*DSQRT(SGG/SGO) + 1.25D0*TRES
         BOS = 0.9759D0 + 12.0D-5 * FF**1.2D0
      END IF
C
      IF (P .LE. PB) THEN
         PVTBO = BOS
      ELSE
         COU   = PVTCOU(P)
         PVTBO = BOS * DEXP(-COU*(P - PB))
      END IF
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION PVTCOU (P)
C-----------------------------------------------------------------------
C     UNDERSATURATED OIL COMPRESSIBILITY, 1/PSI (VAZQUEZ-BEGGS).
C-----------------------------------------------------------------------
      INCLUDE 'PETRO.INC'
      RSB = PVTRS(PB)
      PVTCOU = (-1433.0D0 + 5.0D0*RSB + 17.2D0*TRES
     &          - 1180.0D0*SGG + 12.61D0*API) / (1.0D5 * MAX(P,PATM))
      IF (PVTCOU .LT. SMALL) PVTCOU = SMALL
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION PVTVIS (P)
C-----------------------------------------------------------------------
C     LIVE OIL VISCOSITY, CP.  BEGGS-ROBINSON DEAD OIL + CHEW-CONNALLY.
C-----------------------------------------------------------------------
      INCLUDE 'PETRO.INC'
C
      ZZ   = 3.0324D0 - 0.02023D0*API
      YY   = 10.0D0**ZZ
      XX   = YY * TRES**(-1.163D0)
      VOD  = 10.0D0**XX - ONE
C
      RS   = PVTRS(MIN(P,PB))
      AA   = 10.715D0 * (RS + 100.0D0)**(-0.515D0)
      BB   = 5.44D0   * (RS + 150.0D0)**(-0.338D0)
      VOB  = AA * VOD**BB
C
      IF (P .LE. PB) THEN
         PVTVIS = VOB
      ELSE
         EXPN   = 2.6D0 * P**1.187D0
     &            * DEXP(-11.513D0 - 8.98D-5*P)
         PVTVIS = VOB * (P/PB)**EXPN
      END IF
      IF (PVTVIS .LT. SMALL) PVTVIS = SMALL
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION PVTZ (P)
C-----------------------------------------------------------------------
C     GAS DEVIATION FACTOR BY HALL-YARBOROUGH.  NEWTON ON THE REDUCED
C     DENSITY Y.  SUTTON CORRELATION FOR THE PSEUDO CRITICALS.
C-----------------------------------------------------------------------
      INCLUDE 'PETRO.INC'
      DATA TOLY /1.0D-10/, ITY /40/
C
      TPC = 169.2D0 + 349.5D0*SGG - 74.0D0*SGG*SGG
      PPC = 756.8D0 - 131.0D0*SGG - 3.6D0*SGG*SGG
      TPR = (TRES + TABS) / TPC
      PPR = P / PPC
      IF (TPR .LT. SMALL) THEN
         IERR = 3
         PVTZ = ONE
         RETURN
      END IF
C
      T  = ONE / TPR
      A  = 0.06125D0 * T * DEXP(-1.2D0*(ONE-T)**2)
      B  = T*(14.76D0 - 9.76D0*T + 4.58D0*T*T)
      C  = T*(90.7D0  - 242.2D0*T + 42.4D0*T*T)
      D  = 2.18D0 + 2.82D0*T
C
      Y = 0.001D0
      DO 300 I = 1, ITY
         Y2 = Y*Y
         Y3 = Y2*Y
         Y4 = Y3*Y
         OM = (ONE-Y)**3
         F  = -A*PPR + (Y + Y2 + Y3 - Y4)/OM - B*Y2 + C*Y**D
         DF = (ONE + 4.0D0*Y + 4.0D0*Y2 - 4.0D0*Y3 + Y4)/(ONE-Y)**4
     &        - 2.0D0*B*Y + C*D*Y**(D-ONE)
         IF (DABS(DF) .LT. SMALL) GO TO 390
         DY = F/DF
         Y  = Y - DY
         IF (Y .LE. ZERO) Y = TOLY
         IF (Y .GE. ONE ) Y = ONE - TOLY
         IF (DABS(DY) .LT. TOLY) GO TO 310
  300 CONTINUE
      IWARN = IWARN + 1
C
  310 PVTZ = A * PPR / Y
      RETURN
C
  390 IERR = 4
      PVTZ = ONE
      RETURN
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE PVTSET (P)
C-----------------------------------------------------------------------
C     EVALUATE THE COMPLETE PVT STATE AT PRESSURE P AND LEAVE IT IN
C     /PVTOUT/.  THIS IS THE ROUTINE SIMCOR AND WELLIB ACTUALLY CALL.
C-----------------------------------------------------------------------
      INCLUDE 'PETRO.INC'
C
      PP   = MAX(P, PATM)
      SGO  = 141.5D0 / (131.5D0 + API)
C
      RSOL = PVTRS (MIN(PP,PB))
      BO   = PVTBO (PP)
      VISO = PVTVIS(PP)
      CO   = PVTCOU(PP)
C
      ZFAC = PVTZ  (PP)
      BG   = 0.00503676D0 * ZFAC * (TRES + TABS) / PP
      CG   = ONE / PP
      CALL GASVIS (PP, ZFAC, VISG)
C
      BW   = ONE / (ONE + 3.0D-6*(PP - PATM))
      VISW = 0.30D0
      CW   = 3.0D-6
C
      RHOO = (350.0D0*SGO + 0.0764D0*SGG*RSOL) / (5.615D0*BO)
      RHOG = 0.0764D0*SGG / BG
      RHOW = 62.4D0*SGW / BW
      RETURN
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE GASVIS (P, Z, VG)
C-----------------------------------------------------------------------
C     LEE-GONZALEZ-EAKIN GAS VISCOSITY, CP.
C-----------------------------------------------------------------------
      INCLUDE 'PETRO.INC'
C
      WM  = 28.9625D0 * SGG
      TR  = TRES + TABS
      RHO = 1.4935D-3 * P * WM / (Z * TR)
      AK  = (9.4D0 + 0.02D0*WM) * TR**1.5D0 / (209.0D0 + 19.0D0*WM + TR)
      X   = 3.5D0 + 986.0D0/TR + 0.01D0*WM
      Y   = 2.4D0 - 0.2D0*X
      VG  = 1.0D-4 * AK * DEXP(X * RHO**Y)
      RETURN
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE PVTERR (ICODE, MSG)
C-----------------------------------------------------------------------
C     RETURN AND CLEAR THE ACCUMULATED ERROR STATE.
C-----------------------------------------------------------------------
      INCLUDE 'PETRO.INC'
      CHARACTER*(*) MSG
C
      ICODE = IERR
      IF (IERR .EQ. 0) THEN
         MSG = 'OK'
      ELSE IF (IERR .EQ. -1) THEN
         MSG = 'API GRAVITY CLAMPED TO 1.0'
      ELSE IF (IERR .EQ. 1) THEN
         MSG = 'GAS SPECIFIC GRAVITY MUST BE POSITIVE'
      ELSE IF (IERR .EQ. 2) THEN
         MSG = 'BUBBLE POINT NEWTON DERIVATIVE VANISHED'
      ELSE IF (IERR .EQ. 3) THEN
         MSG = 'PSEUDO REDUCED TEMPERATURE NON POSITIVE'
      ELSE IF (IERR .EQ. 4) THEN
         MSG = 'HALL-YARBOROUGH DERIVATIVE VANISHED'
      ELSE
         MSG = 'UNKNOWN PVT ERROR'
      END IF
      IERR = 0
      RETURN
      END
